"""
Jooble API scraper.

Jooble is a free job aggregator API that covers Vietnamese job postings.
API docs: https://jooble.org/api/about
Endpoint: POST https://jooble.org/api/{api_key}
"""

from __future__ import annotations

import html
import re
from typing import Optional

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)


class JoobleScraper(BaseScraper):
    """Scraper for Jooble API (free, aggregates from multiple VN sites)."""

    BASE_URL = "https://jooble.org/api"

    def __init__(self):
        super().__init__(source=JobSource.JOOBLE)
        if not self.settings.jooble_api_key:
            logger.warning("Jooble API key not configured. Register at https://jooble.org/api/about")

    PAGE_SIZE = 20

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: str | None = None,
    ) -> list[RawJob]:
        """Scrape jobs from Jooble API.

        Args:
            keyword: Search keyword (e.g., "data engineer").
            max_pages: Max pages to fetch (each page ~20 jobs).
            location: City or region; defaults to all of Vietnam.

        Jooble returns relevance-ordered results and has no documented sort
        parameter, so there is no ``scrape_latest``.
        """
        if not self.settings.jooble_api_key:
            logger.error("Jooble API key not set. Skipping.")
            self.last_error = "no_api_key"
            return []

        max_pages = max_pages or self.settings.scrape_max_pages
        if self.live_mode:
            max_pages = 1
        all_jobs: list[RawJob] = []
        url = f"{self.BASE_URL}/{self.settings.jooble_api_key}"

        for page in range(1, max_pages + 1):
            payload = {
                "keywords": keyword,
                "location": location or "Vietnam",
                "page": str(page),
                "ResultOnPage": str(self.PAGE_SIZE),
            }

            try:
                response = self._post(url, json_data=payload)
                data = response.json()

                if "jobs" not in data:
                    # 200 without the documented envelope: the API changed.
                    if page == 1:
                        self.mark_layout_changed()
                    break
                jobs_data = data.get("jobs") or []
                if not jobs_data:
                    logger.info(f"No more jobs on page {page}, stopping.")
                    break

                for job_data in jobs_data:
                    job = self.parse_job(job_data)
                    if job:
                        all_jobs.append(job)

                total = data.get("totalCount", 0) or 0
                logger.info(
                    f"Jooble page {page}: {len(jobs_data)} jobs (total: {total})",
                    extra={"source": self.source.value},
                )
                if page * self.PAGE_SIZE >= total:
                    break

            except Exception as e:
                logger.error(f"Jooble page {page} failed: {e}", exc_info=True)
                break

        return all_jobs

    @staticmethod
    def _clean(text: object) -> str:
        """Jooble snippets carry <b> highlights, entities and ``&nbsp;``."""
        text = re.sub(r"<[^>]+>", "", str(text or ""))
        return re.sub(r"\s+", " ", html.unescape(text)).strip()

    def parse_job(self, raw_data: dict) -> Optional[RawJob]:
        """Parse a Jooble API response item into RawJob.

        Jooble response format:
        {
            "title": "Data Engineer",
            "location": "Ho Chi Minh City",
            "snippet": "Job description...",
            "salary": "$1000-2000",
            "source": "careerlink.vn",
            "type": "Full-time",
            "link": "https://...",
            "company": "ABC Corp",
            "updated": "2026-06-14T10:00:00Z",
            "id": 123456
        }
        """
        try:
            title = self._clean(raw_data.get("title"))
            if not title:
                return None

            return RawJob(
                title=title,
                company=self._clean(raw_data.get("company")),
                location=self._clean(raw_data.get("location")),
                salary_raw=self._clean(raw_data.get("salary")),
                description=self._clean(raw_data.get("snippet")),
                job_type_raw=self._clean(raw_data.get("type")),
                source=JobSource.JOOBLE,
                source_url=str(raw_data.get("link") or "").strip(),
                posted_at_raw=str(raw_data.get("updated") or ""),
            )
        except Exception as e:
            logger.warning(f"Failed to parse Jooble job: {e}")
            return None
