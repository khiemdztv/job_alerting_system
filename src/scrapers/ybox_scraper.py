"""
Ybox.vn scraper.

YBox server-renders the whole current job list (about 42 posts, ~300 KB) into
``window.__INITIAL_ADS__`` and ignores ``keyword``/``page`` query parameters
(verified: the payload is identical with or without them, ``/tim-kiem?q=`` too),
so there is no cheaper URL; the keyword is applied locally like the site does.
Postings are mostly internships and part-time roles for students.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.matcher.search import relevance
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://ybox.vn"

_JS_DATE = re.compile(
    r"^\w{3} (\w{3} \d{1,2} \d{4} \d{2}:\d{2}:\d{2}) GMT([+-]\d{4})"
)
_SALARY = re.compile(
    r"\d[\d.,]*(?:\s*(?:-|–|đến)\s*\d[\d.,]*)?\s*(?:vn[đd]|triệu|tr\b)"
    r"(?:\s*/\s*(?:tháng|thang))?",
    re.IGNORECASE,
)

LOCATION_ALIASES = {
    "ha noi": ("hn", "ha noi", "hanoi"),
    "ho chi minh": ("hcm", "ho chi minh", "sai gon", "sg", "tphcm"),
    "da nang": ("dn", "da nang"),
}


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()


def _iso_date(value: str) -> str:
    """Turn JS ``Date.toString()`` text into ISO 8601 so the ETL can parse it."""
    match = _JS_DATE.match((value or "").strip())
    if not match:
        return value or ""
    try:
        parsed = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%b %d %Y %H:%M:%S %z")
    except ValueError:
        return value
    return parsed.isoformat()


def _job_types(title: str) -> str:
    low = title.lower()
    kinds = []
    if any(k in low for k in ("thực tập", "intern", "sinh viên", "trainee", "fresher")):
        kinds.append("internship")
    part_time = ("part-time", "part time", "parttime", "bán thời gian", "cộng tác viên", "ctv")
    if any(k in low for k in part_time):
        kinds.append("part-time")
    if any(k in low for k in ("full-time", "full time", "fulltime", "toàn thời gian")):
        kinds.append("full-time")
    return ", ".join(kinds)


class YBoxScraper(BaseScraper):
    """Scraper for YBOX (ybox.vn)."""

    SEARCH_URL = f"{BASE_URL}/tuyen-dung-viec-lam-tk-c1"
    supports_latest = True

    def __init__(self):
        super().__init__(source=JobSource.YBOX)

    def _load_posts(self) -> list[dict] | None:
        """Download the list page and return its posts, or None on layout change."""
        response = self._get(self.SEARCH_URL)
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        for script in soup.find_all("script"):
            if script.string and "window.__INITIAL_ADS__" in script.string:
                content = script.string.strip()
                start_idx = content.find("{")
                end_idx = content.rfind("}")
                if start_idx == -1 or end_idx == -1:
                    continue
                try:
                    ads_data = json.loads(content[start_idx : end_idx + 1])
                except ValueError as je:
                    logger.error(f"Failed to parse Ybox INITIAL_ADS JSON: {je}")
                    continue
                edges = (ads_data.get("Ads") or {}).get("edges") or []
                posts = [e.get("post") for e in edges if e and e.get("post")]
                return posts or None
        logger.warning("Could not find or parse Ybox INITIAL_ADS script tag.")
        return None

    @staticmethod
    def _matches_location(job: RawJob, location: str) -> bool:
        wanted = _fold(location)
        aliases = next(
            (a for key, a in LOCATION_ALIASES.items() if wanted == key or wanted in a),
            (wanted,),
        )
        tokens = set(_fold(job.location).split()) | set(_fold(job.title).split())
        folded = _fold(job.location)
        # Online / nationwide postings fit any city.
        if "online" in tokens or ("toan" in tokens and "quoc" in tokens):
            return True
        return any(alias in tokens or alias in folded for alias in aliases)

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: str | None = None,
    ) -> list[RawJob]:
        """Scrape YBOX job listings, filtering the single list page locally."""
        posts = self._load_posts()
        if posts is None:
            self.mark_layout_changed()
            return []
        logger.info(f"Ybox page returned {len(posts)} ads. Filtering for '{keyword}'...")

        jobs: list[RawJob] = []
        for post in posts:
            if not relevance(
                {"title": post.get("title", ""), "description": post.get("summary", "")},
                keyword,
            ):
                continue
            job = self.parse_job(post)
            if job and (not location or self._matches_location(job, location)):
                jobs.append(job)
        logger.info(
            f"YBox: {len(jobs)} jobs matched keyword '{keyword}'",
            extra={"source": self.source.value},
        )
        return jobs

    def scrape_latest(self, max_pages: Optional[int] = None) -> list[RawJob]:
        """Every post on the list page, newest first."""
        posts = self._load_posts()
        if posts is None:
            self.mark_layout_changed()
            return []
        jobs = [job for post in posts if (job := self.parse_job(post))]
        jobs.sort(key=lambda j: j.posted_at_raw, reverse=True)  # ISO strings sort by time
        return jobs

    def parse_job(self, post: dict) -> Optional[RawJob]:
        """Parse Ybox post dictionary to RawJob."""
        try:
            title = (post.get("title") or "").strip()
            slug = post.get("slug") or ""
            if not title or not slug:
                return None

            source_url = f"{BASE_URL}/tuyen-dung/{slug}"

            publisher = post.get("publisher") or {}
            company = publisher.get("fullName") or publisher.get("username") or "Ybox Employer"

            # Location is the bracketed prefix: "[HCM]", "[HN/Hưng Yên]", "[Online-HCM]".
            location = "Việt Nam"
            loc_match = re.search(r"^\[([^\]]+)\]", title)
            if loc_match:
                location = loc_match.group(1).strip()
            else:
                low = title.lower()
                if "hcm" in low or "hồ chí minh" in low or "sài gòn" in low:
                    location = "Hồ Chí Minh"
                elif re.search(r"\bhn\b", low) or "hà nội" in low:
                    location = "Hà Nội"

            salary = ""
            salary_match = _SALARY.search(title)
            if salary_match:
                salary = salary_match.group(0).strip()

            description = (post.get("summary") or "").strip()
            posted = post.get("publishedAt") or post.get("acceptedAt") or ""

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                description=description,
                job_type_raw=_job_types(title),
                source=JobSource.YBOX,
                source_url=source_url,
                posted_at_raw=_iso_date(posted),
            )

        except Exception as e:
            logger.warning(f"Failed to parse Ybox job: {e}")
            return None
