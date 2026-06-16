"""
TimViecNhanh.com scraper.

NOTE: TimViecNhanh.com is the same platform as ViecLam24h.vn.
The canonical URL confirms: https://vieclam24h.vn/
This scraper skips to avoid duplicate data.
"""

from __future__ import annotations

from typing import Optional

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.scrapers.vieclam24h_scraper import ViecLam24hScraper

logger = get_logger(__name__)


class TimViecNhanhScraper(ViecLam24hScraper):
    """Scraper for TimViecNhanh.com (same platform as ViecLam24h).

    TimViecNhanh redirects to ViecLam24h. Skips scraping to avoid duplicates.
    """

    def __init__(self):
        super(ViecLam24hScraper, self).__init__(source=JobSource.TIMVIECNHANH)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        logger.info(
            "TimViecNhanh is the same platform as ViecLam24h. Skipping to avoid duplicates.",
            extra={"source": self.source.value},
        )
        return []
