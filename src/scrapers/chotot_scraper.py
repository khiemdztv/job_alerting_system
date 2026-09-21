"""
Chợ Tốt (Việc Làm Tốt) scraper.

Uses the public JSON API from Chợ Tốt's job listing service.
Covers: blue-collar, retail, logistics, construction, F&B — industries
that are underrepresented in white-collar job boards.

API: GET https://gateway.chotot.com/v1/public/ad-listing
Params: cg=13000 (jobs category), q=keyword, limit=50, o=offset, st=s,k
Response: JSON with ads[] array containing job data.
"""

from __future__ import annotations

from typing import Optional

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)


class ChototScraper(BaseScraper):
    """Scraper for Việc Làm Tốt (Chợ Tốt) — public JSON API, multi-industry."""

    API_URL = "https://gateway.chotot.com/v1/public/ad-listing"

    def __init__(self):
        super().__init__(source=JobSource.CHOTOT)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Scrape jobs from Chợ Tốt API.

        Args:
            keyword: Search keyword (e.g., "kế toán", "bán hàng").
            max_pages: Max pages to fetch (each page ~50 jobs).

        Returns:
            List of RawJob objects.
        """
        max_pages = max_pages or self.settings.scrape_max_pages
        all_jobs: list[RawJob] = []
        limit = 50

        for page in range(max_pages):
            params = {
                "cg": 13000,        # Jobs category
                "q": keyword,
                "limit": limit,
                "o": page * limit,  # Offset
                "st": "s,k",       # Sort: relevance
            }

            try:
                response = self._get(self.API_URL, params=params)
                data = response.json()
                ads = data.get("ads", [])

                if not ads:
                    logger.info(f"No more jobs on page {page + 1}, stopping.")
                    break

                for ad in ads:
                    job = self.parse_job(ad)
                    if job:
                        all_jobs.append(job)

                total = data.get("total", 0)
                logger.info(
                    f"Chợ Tốt page {page + 1}: {len(ads)} ads (total: {total})",
                    extra={"source": self.source.value},
                )

                # Stop if we've fetched all available
                if (page + 1) * limit >= total:
                    break

            except Exception as e:
                logger.error(f"Chợ Tốt page {page + 1} failed: {e}", exc_info=True)
                break

        return all_jobs

    def parse_job(self, ad: dict) -> Optional[RawJob]:
        """Parse a Chợ Tốt ad JSON object into RawJob.

        Ad format (key fields):
        {
            "list_id": 123456789,
            "subject": "Tuyển nhân viên bán hàng",
            "body": "Mô tả công việc...",
            "company_name": "Công ty ABC",
            "account_name": "Nguyễn Văn A",
            "area_name": "Quận 1, Hồ Chí Minh",
            "region_name": "Hồ Chí Minh",
            "salary_min": 8000000,
            "salary_max": 15000000,
            "job_type_name": "Toàn thời gian",
            "list_time": 1720300000000,  # epoch milliseconds
        }
        """
        try:
            title = (ad.get("subject") or "").strip()
            if not title:
                return None

            # Build source URL
            list_id = ad.get("list_id", "")
            source_url = f"https://www.chotot.com/{list_id}.htm" if list_id else ""

            # Company: prefer company_name, fallback to account_name
            company = ad.get("company_name") or ad.get("account_name", "")

            # Location: prefer area_name (more specific), fallback to region_name
            location = ad.get("area_name", "") or ad.get("region_name", "")

            # Salary from structured fields
            salary = ""
            salary_min = ad.get("salary_min")
            salary_max = ad.get("salary_max")
            if salary_min or salary_max:
                s_min = (salary_min or 0) / 1_000_000
                s_max = (salary_max or 0) / 1_000_000
                if s_min and s_max and s_min != s_max:
                    salary = f"{s_min:g} - {s_max:g} triệu"
                elif s_max:
                    salary = f"{s_max:g} triệu"
                elif s_min:
                    salary = f"Từ {s_min:g} triệu"

            # Description
            description = ad.get("body", "") or ad.get("job_type_name", "")

            # Job type
            job_type_raw = ad.get("job_type_name", "")

            # Posted at: epoch milliseconds
            posted_at_raw = ""
            list_time = ad.get("list_time")
            if list_time:
                posted_at_raw = str(list_time)  # Will be parsed in Transformer._parse_date

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                description=description,
                job_type_raw=job_type_raw,
                source=JobSource.CHOTOT,
                source_url=source_url,
                posted_at_raw=posted_at_raw,
            )

        except Exception as e:
            logger.warning(f"Failed to parse Chợ Tốt ad: {e}")
            return None
