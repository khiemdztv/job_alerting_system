"""
ITviec.com scraper.

Search: GET https://itviec.com/it-jobs?query=<kw>&page=<n> (redirects to
/it-jobs/<kw-slug>); a city goes in the path: /it-jobs/<city>?query=<kw>.
Listings come newest first and hold 20 cards per page.  A keyword with no
match answers HTTP 404 (a legitimate empty result, not an error).

Verified selectors (div.job-card, Stimulus data attributes):
- slug: data-search--job-selection-job-slug-value
- title: h3; posted: span.small-text ("Posted 2 hours ago")
- company: a[href^=/companies/] text (logo link carries a title attribute)
- salary: div.salary (anonymous users only see "Sign in to view salary")
- work mode (At office / Hybrid / Remote) and location: div.text-rich-grey
  followed by a div whose title attribute is the city
- skills: a.itag
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://itviec.com"

WORK_MODES = ("at office", "hybrid", "remote")
CITY_SLUGS = {
    "ha noi": "ha-noi",
    "ho chi minh": "ho-chi-minh-hcm",
    "da nang": "da-nang",
}


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", text)).strip()


class ITviecScraper(BaseScraper):
    """Scraper for ITviec.com (IT-focused job board, has CF scripts but doesn't block)."""

    SEARCH_URL = f"{BASE_URL}/it-jobs"
    supports_latest = True

    def __init__(self):
        super().__init__(source=JobSource.ITVIEC)

    @staticmethod
    def city_slug(location: str | None) -> str | None:
        if not location:
            return None
        key = _fold(location)
        if key in ("hcm", "tp hcm", "sai gon"):
            key = "ho chi minh"
        elif key == "hanoi":
            key = "ha noi"
        return CITY_SLUGS.get(key)

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: str | None = None,
    ) -> list[RawJob]:
        """Scrape ITviec job listings (newest first).

        Args:
            keyword: Search keyword.
            max_pages: Max pages to scrape (20 jobs each).
            location: Optional city (Hà Nội, Hồ Chí Minh, Đà Nẵng).
        """
        return self._fetch(keyword, max_pages, location)

    def scrape_latest(self, max_pages: Optional[int] = None) -> list[RawJob]:
        """Newest IT jobs without a keyword."""
        return self._fetch("", max_pages, None)

    def _fetch(
        self, keyword: str, max_pages: Optional[int], location: str | None
    ) -> list[RawJob]:
        max_pages = max_pages or min(self.settings.scrape_max_pages, 3)  # Gentle on ITviec
        if self.live_mode:
            max_pages = 1
        all_jobs: list[RawJob] = []
        city = self.city_slug(location)
        url = f"{self.SEARCH_URL}/{city}" if city else self.SEARCH_URL

        for page in range(1, max_pages + 1):
            params: dict = {"page": page}
            if keyword:
                params["query"] = keyword

            try:
                response = self._get(url, params=params)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 403:
                    logger.warning("ITviec returned 403 (Cloudflare protection active).")
                elif status == 404:
                    # ITviec answers 404 for a keyword nothing matches: just empty.
                    self.last_error = ""
                    logger.info(f"ITviec: no jobs for '{keyword}' (404).")
                else:
                    logger.error(f"ITviec page {page} failed: {e}", exc_info=True)
                break
            except Exception as e:
                logger.error(f"ITviec page {page} failed: {e}", exc_info=True)
                break

            soup = BeautifulSoup(response.text, "html.parser")
            job_cards = soup.find_all(class_="job-card")
            if not job_cards:
                if page == 1:
                    self.mark_layout_changed()
                logger.info(f"No more jobs on page {page}, stopping.")
                break

            parsed = 0
            for card in job_cards:
                job = self.parse_job(card)
                if job:
                    all_jobs.append(job)
                    parsed += 1
            if page == 1 and not parsed:
                self.mark_layout_changed()

            logger.info(
                f"ITviec page {page}: {len(job_cards)} jobs found",
                extra={"source": self.source.value},
            )
            if len(job_cards) < 20:
                break

        return all_jobs

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        """Parse an ITviec job-card element."""
        try:
            title_el = raw_data.find("h3")
            title = title_el.get_text(" ", strip=True) if title_el else ""
            if not title:
                return None

            slug = raw_data.get("data-search--job-selection-job-slug-value", "")
            if not slug and title_el:
                link = title_el.find("a", href=True)
                if link:
                    slug = link["href"].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            source_url = f"{BASE_URL}/it-jobs/{slug}" if slug else ""

            # Company: text of the /companies/ link; the logo link only has a title.
            company = ""
            for link in raw_data.find_all("a", href=True):
                if "/companies/" in link["href"]:
                    company = link.get_text(strip=True)
                    if company:
                        break
            if not company:
                logo = raw_data.find("a", class_="logo-employer-card")
                company = (logo.get("title") or "").strip() if logo else ""

            posted_at_raw = ""
            date_el = raw_data.find("span", class_="small-text")
            if date_el:
                text = re.sub(r"\s+", " ", date_el.get_text(" ", strip=True))
                if "posted" in text.lower():
                    posted_at_raw = text

            # Salary is hidden behind login for anonymous visitors.
            salary = ""
            salary_el = raw_data.find(class_="salary")
            if salary_el:
                text = re.sub(r"\s+", " ", salary_el.get_text(" ", strip=True))
                if text and "sign in" not in text.lower() and "đăng nhập" not in text.lower():
                    salary = text

            # Work mode and location live in the same row.
            job_type = ""
            location = ""
            for el in raw_data.find_all("div", class_="text-rich-grey"):
                text = el.get_text(strip=True)
                if not job_type and text.lower() in WORK_MODES:
                    job_type = text
                if el.get("title") and not location:
                    location = el["title"].strip()
            if not location:
                card_text = _fold(raw_data.get_text(" ", strip=True))
                for key, label in (
                    ("ha noi", "Ha Noi"),
                    ("ho chi minh", "Ho Chi Minh"),
                    ("da nang", "Da Nang"),
                ):
                    if key in card_text:
                        location = label
                        break

            tags = [
                a.get_text(strip=True)
                for a in raw_data.find_all("a", class_="itag")
                if a.get_text(strip=True)
            ]

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                description=", ".join(tags),
                job_type_raw=job_type,
                source=JobSource.ITVIEC,
                source_url=source_url,
                posted_at_raw=posted_at_raw,
            )

        except Exception as e:
            logger.warning(f"Failed to parse ITviec job: {e}")
            return None
