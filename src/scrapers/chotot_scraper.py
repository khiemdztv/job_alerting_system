"""
Chợ Tốt (Việc Làm Tốt) scraper.

Uses the public JSON API from Chợ Tốt's job listing service.
Covers: blue-collar, retail, logistics, construction, F&B — industries
that are underrepresented in white-collar job boards.

API: GET https://gateway.chotot.com/v1/public/ad-listing
Params: cg=13000 (jobs category), q=keyword, limit<=50 (higher values are capped
at 50), o=offset, region_v2 (12000 Ha Noi, 13000 Ho Chi Minh).  Without a sort
parameter the API already returns the newest ads first (by ``list_time``).
Response: JSON with ads[] array containing job data and ``total`` (capped at 10000).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)


class ChototScraper(BaseScraper):
    """Scraper for Việc Làm Tốt (Chợ Tốt) — public JSON API, multi-industry."""

    API_URL = "https://gateway.chotot.com/v1/public/ad-listing"
    PAGE_SIZE = 50  # API maximum
    supports_latest = True

    # region_v2 codes verified against the live API.
    REGIONS = {
        "ha noi": 12000,
        "hanoi": 12000,
        "ho chi minh": 13000,
        "hcm": 13000,
        "tp hcm": 13000,
        "sai gon": 13000,
    }

    # contract_type values that are unambiguous.
    CONTRACT_TYPES = {1: "Toàn thời gian", 2: "Bán thời gian"}

    def __init__(self):
        super().__init__(source=JobSource.CHOTOT)

    @classmethod
    def region_code(cls, location: str | None) -> int | None:
        if not location:
            return None
        key = unicodedata.normalize("NFD", location.lower().replace("đ", "d"))
        key = "".join(c for c in key if not unicodedata.combining(c))
        key = re.sub(r"[^a-z ]", " ", key)
        key = re.sub(r"\s+", " ", key).strip()
        return cls.REGIONS.get(key)

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: str | None = None,
    ) -> list[RawJob]:
        """Scrape jobs from Chợ Tốt API.

        Args:
            keyword: Search keyword (e.g., "kế toán", "bán hàng").
            max_pages: Max pages to fetch (each page up to 50 jobs).
            location: Optional city ("Hà Nội", "Hồ Chí Minh"); others are ignored.
        """
        return self._fetch(keyword, max_pages, location)

    def scrape_latest(self, max_pages: Optional[int] = None) -> list[RawJob]:
        """Newest job ads across all of Vietnam, no keyword."""
        return self._fetch("", max_pages, None)

    def _fetch(
        self, keyword: str, max_pages: Optional[int], location: str | None
    ) -> list[RawJob]:
        max_pages = max_pages or self.settings.scrape_max_pages
        if self.live_mode:
            max_pages = 1
        all_jobs: list[RawJob] = []
        limit = self.PAGE_SIZE
        region = self.region_code(location)

        for page in range(max_pages):
            params: dict = {
                "cg": 13000,        # Jobs category
                "limit": limit,
                "o": page * limit,  # Offset
            }
            if keyword:
                params["q"] = keyword
            if region:
                params["region_v2"] = region

            try:
                response = self._get(self.API_URL, params=params)
                data = response.json()
                ads = data.get("ads", [])

                if not ads:
                    if page == 0 and data.get("total", 0) == 0 and "ads" not in data:
                        # 200 but the payload has no ads key: API shape changed.
                        self.mark_layout_changed()
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

    @staticmethod
    def _salary_text(ad: dict) -> str:
        """Readable salary from min/max_salary (VND), else the API's price_string."""
        s_min = ad.get("min_salary") or ad.get("salary_min") or 0
        s_max = ad.get("max_salary") or ad.get("salary_max") or 0
        try:
            s_min = float(s_min) / 1_000_000
            s_max = float(s_max) / 1_000_000
        except (TypeError, ValueError):
            s_min = s_max = 0
        if s_min and s_max and s_min != s_max:
            return f"{s_min:g} - {s_max:g} triệu"
        if s_max:
            return f"{s_max:g} triệu"
        if s_min:
            return f"Từ {s_min:g} triệu"
        price = ad.get("price_string") or ""
        return price.strip() if re.search(r"\d", price) else ""

    def parse_job(self, ad: dict) -> Optional[RawJob]:
        """Parse a Chợ Tốt ad JSON object into RawJob.

        Key fields (verified live): subject, body, company_name, account_name,
        area_name, region_name, min_salary/max_salary (VND), price_string,
        contract_type (1 full-time, 2 part-time), list_time/orig_list_time
        (epoch ms; list_time moves on every bump, orig_list_time is the post date),
        list_id/ad_id.
        """
        try:
            title = (ad.get("subject") or "").strip()
            if not title:
                return None

            list_id = ad.get("list_id") or ad.get("ad_id") or ""
            source_url = f"https://www.chotot.com/{list_id}.htm" if list_id else ""

            company = (ad.get("company_name") or ad.get("account_name") or "").strip()

            area = (ad.get("area_name") or "").strip()
            region = (ad.get("region_name") or "").strip()
            location = ", ".join(p for p in (area, region) if p)

            job_type_raw = ad.get("job_type_name") or self.CONTRACT_TYPES.get(
                ad.get("contract_type"), ""
            )
            description = ad.get("body") or ""

            ts = ad.get("orig_list_time") or ad.get("list_time")
            posted_at_raw = str(ts) if ts else ""  # epoch ms, parsed by the Transformer

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=self._salary_text(ad),
                description=description,
                job_type_raw=job_type_raw,
                source=JobSource.CHOTOT,
                source_url=source_url,
                posted_at_raw=posted_at_raw,
            )

        except Exception as e:
            logger.warning(f"Failed to parse Chợ Tốt ad: {e}")
            return None
