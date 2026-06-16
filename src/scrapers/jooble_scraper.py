"""
Jooble API scraper.

Jooble is a free job aggregator API that covers Vietnamese job postings.
API docs: https://jooble.org/api/about
Endpoint: POST https://jooble.org/api/{api_key}
"""

from __future__ import annotations

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

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Scrape jobs from Jooble API.

        Args:
            keyword: Search keyword (e.g., "data engineer").
            max_pages: Max pages to fetch (each page ~20 jobs).

        Returns:
            List of RawJob objects.
        """
        if not self.settings.jooble_api_key:
            logger.error("Jooble API key not set. Skipping.")
            return []

        max_pages = max_pages or self.settings.scrape_max_pages
        all_jobs: list[RawJob] = []
        url = f"{self.BASE_URL}/{self.settings.jooble_api_key}"

        for page in range(1, max_pages + 1):
            payload = {
                "keywords": keyword,
                "location": "Vietnam",
                "page": str(page),
            }

            try:
                response = self._post(url, json_data=payload)
                data = response.json()

                jobs_data = data.get("jobs", [])
                if not jobs_data:
                    logger.info(f"No more jobs on page {page}, stopping.")
                    break

                for job_data in jobs_data:
                    job = self.parse_job(job_data)
                    if job:
                        all_jobs.append(job)

                total = data.get("totalCount", 0)
                logger.info(
                    f"Jooble page {page}: {len(jobs_data)} jobs (total: {total})",
                    extra={"source": self.source.value},
                )

            except Exception as e:
                logger.error(f"Jooble page {page} failed: {e}", exc_info=True)
                break

        return all_jobs

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
            title = raw_data.get("title", "").strip()
            if not title:
                return None

            return RawJob(
                title=title,
                company=raw_data.get("company", ""),
                location=raw_data.get("location", ""),
                salary_raw=raw_data.get("salary", ""),
                description=raw_data.get("snippet", ""),
                job_type_raw=raw_data.get("type", ""),
                source=JobSource.JOOBLE,
                source_url=raw_data.get("link", ""),
                posted_at_raw=raw_data.get("updated", ""),
            )
        except Exception as e:
            logger.warning(f"Failed to parse Jooble job: {e}")
            return None
