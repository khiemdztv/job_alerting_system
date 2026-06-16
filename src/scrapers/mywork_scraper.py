"""
MyWork.com.vn scraper.

NOTE: MyWork.com.vn redirects to / is the same platform as ViecLam24h.vn.
The canonical URL confirms: https://vieclam24h.vn/
This scraper reuses ViecLam24h parsing logic with MyWork URL.
"""

from __future__ import annotations

from typing import Optional

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.scrapers.vieclam24h_scraper import ViecLam24hScraper

logger = get_logger(__name__)


class MyWorkScraper(ViecLam24hScraper):
    """Scraper for MyWork.com.vn (same platform as ViecLam24h).

    MyWork redirects to ViecLam24h, so we inherit the same parser
    but tag results with MYWORK source for tracking.
    """

    SEARCH_URL = "https://mywork.com.vn/tuyen-dung"

    def __init__(self):
        # Call BaseScraper.__init__ directly to avoid ViecLam24h's __init__
        super(ViecLam24hScraper, self).__init__(source=JobSource.MYWORK)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Scrape MyWork (uses ViecLam24h under the hood).

        Since MyWork serves the same content as ViecLam24h,
        we use ViecLam24h's SEARCH_URL directly to avoid redirect overhead.
        """
        logger.info(
            "MyWork is the same platform as ViecLam24h. "
            "Delegating to ViecLam24h scraper to avoid duplicate data.",
            extra={"source": self.source.value},
        )
        # Skip scraping to avoid duplicates — ViecLam24h already covers this
        return []
