"""JobsGO live search scraper.

JobsGO serves current listings to search-engine crawlers while its normal
browser endpoint uses a Cloudflare challenge.  We use the documented public
listing pages and parse only job cards already present in the HTML.

Verified card markup (October 2026):
- Container: div.job-card
- Title: h3.job-title a[title] (text may be prefixed by a "HOT" badge span)
- Company: a.company-title
- Salary and city: spans inside div.text-primary, separated by a "|" span
- Updated: span.badge[title="Thời gian cập nhật"], e.g. "18 phút trước" (last
  refresh, not the original posting date)
- Job type: span.badge[title="Loại hình"], e.g. "Full-time"

Audit 30 Oct 2026: every JobsGO URL (listing, robots.txt, /rss) answers a normal
browser User-Agent with a Cloudflare "Just a moment..." 403.  An earlier version
got through by sending a Googlebot User-Agent; that is crawler impersonation,
so it was removed and the source is disabled in the registry until JobsGO can
be read legitimately (official feed, partner API, or an allowed runner).  The
parser is kept so re-enabling is a one-line registry change.  A 200 page
without job cards sets ``last_error = "layout_changed"``.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.common.text_utils import slugify_vn
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://jobsgo.vn"

_SALARY = re.compile(r"triệu|VNĐ|USD|thỏa thuận", re.I)
_CITY = re.compile(r"Hồ Chí Minh|TP\.?\s*HCM|Hà Nội|Đà Nẵng|Bình Dương|Đồng Nai|Remote", re.I)
_AGO = re.compile(r"\d+\s+(?:phút|giờ|ngày|tuần|tháng)\s+trước", re.I)
_NO_RESULTS = re.compile(r"không tìm thấy|không có (?:kết quả|việc làm)|0 việc làm", re.I)


def _canonical(url: str) -> str:
    """Drop tracking parameters such as ``?ref_component=list``."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class JobsGoScraper(BaseScraper):
    def __init__(self):
        super().__init__(source=JobSource.JOBSGO)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        max_pages = 1 if self.live_mode else (max_pages or self.settings.scrape_max_pages)
        jobs: list[RawJob] = []
        seen: set[str] = set()
        path = f"{BASE_URL}/viec-lam-{slugify_vn(keyword)}.html"
        for page in range(1, max_pages + 1):
            try:
                response = self._get(path, params={"page": page} if page > 1 else None)
            except (requests.RequestException, TimeoutError) as exc:
                logger.warning(f"JobsGO page {page} failed: {exc}")
                break
            soup = BeautifulSoup(response.text, "html.parser")
            parsed = [job for card in soup.select(".job-card") if (job := self.parse_job(card))]
            if not parsed:
                if page == 1 and not _NO_RESULTS.search(soup.get_text(" ", strip=True)):
                    self.mark_layout_changed()
                break
            new = [job for job in parsed if job.source_url not in seen]
            seen.update(job.source_url for job in new)
            jobs.extend(new)
            if not new:
                break
        return jobs

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        title_link = raw_data.select_one(".job-title a[href]")
        if not title_link:
            return None
        title = (title_link.get("title") or "").strip() or title_link.get_text(" ", strip=True)
        company_el = raw_data.select_one(".company-title")
        company = company_el.get_text(" ", strip=True) if company_el else ""
        source_url = _canonical(urljoin(BASE_URL, title_link.get("href", "")))

        # Salary and city live in one metadata row; the job title is never consulted
        # for the city, because titles such as "... tại Hồ Chí Minh" misled the old parser.
        meta = raw_data.select_one(".text-primary")
        meta_parts = [
            part
            for span in (meta.find_all("span") if meta else [])
            if (part := span.get_text(" ", strip=True)) and part != "|"
        ]
        salary = next((part for part in meta_parts if _SALARY.search(part)), "")
        location = next((part for part in reversed(meta_parts) if part != salary), "")

        text_parts = [
            part.strip()
            for part in raw_data.stripped_strings
            if part.strip() and part.strip() != title
        ]
        if not salary:
            salary = next((part for part in text_parts if _SALARY.search(part)), "")
        if not location:
            location = next((part for part in text_parts if _CITY.search(part)), "")

        posted_el = raw_data.find("span", title="Thời gian cập nhật")
        posted = posted_el.get_text(" ", strip=True) if posted_el else ""
        if not posted:
            posted = next((part for part in text_parts if _AGO.search(part)), "")

        type_el = raw_data.find("span", title="Loại hình")
        job_type = type_el.get_text(" ", strip=True) if type_el else ""

        return RawJob(
            title=title,
            company=company,
            location=location,
            salary_raw=salary,
            description=" ".join(text_parts),
            job_type_raw=job_type,
            source=JobSource.JOBSGO,
            source_url=source_url,
            posted_at_raw=posted,
        )
