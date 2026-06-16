"""
Abstract base scraper for ViecLamBot.

All source-specific scrapers inherit from this base class,
ensuring a consistent interface and shared functionality.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import requests

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.config import get_settings

logger = get_logger(__name__)


class BaseScraper(ABC):
    """Abstract base class for all job scrapers.

    Provides:
    - HTTP session with retry logic
    - Rate limiting between requests
    - Standardized scrape interface
    - Error handling and logging
    """

    def __init__(self, source: JobSource):
        self.source = source
        self.settings = get_settings()
        self.session = self._create_session()
        self._last_request_time: float = 0.0

    def _create_session(self) -> requests.Session:
        """Create a requests session with proper headers and retry config."""
        session = requests.Session()
        session.headers.update(self.settings.scraper_headers)

        # Retry adapter
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.adapters.Retry(
                total=3,
                backoff_factor=1.0,
                status_forcelist=[429, 500, 502, 503, 504],
            )
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    def _rate_limit(self) -> None:
        """Enforce delay between requests to avoid being blocked."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.settings.scrape_delay_seconds:
            sleep_time = self.settings.scrape_delay_seconds - elapsed
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    def _get(self, url: str, params: Optional[dict] = None, **kwargs) -> requests.Response:
        """Make a rate-limited GET request.

        Args:
            url: Target URL.
            params: Query parameters.
            **kwargs: Additional arguments for requests.get.

        Returns:
            Response object.

        Raises:
            requests.RequestException: On network errors after retries.
        """
        self._rate_limit()
        logger.info(
            f"Scraping {url}",
            extra={"source": self.source.value},
        )
        response = self.session.get(url, params=params, timeout=15, **kwargs)
        response.raise_for_status()
        return response

    def _post(self, url: str, json_data: Optional[dict] = None, **kwargs) -> requests.Response:
        """Make a rate-limited POST request.

        Args:
            url: Target URL.
            json_data: JSON body.
            **kwargs: Additional arguments for requests.post.

        Returns:
            Response object.
        """
        self._rate_limit()
        logger.info(
            f"POST {url}",
            extra={"source": self.source.value},
        )
        response = self.session.post(url, json=json_data, timeout=15, **kwargs)
        response.raise_for_status()
        return response

    @abstractmethod
    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Scrape job listings for a given keyword.

        Args:
            keyword: Job search keyword (e.g., "data engineer").
            max_pages: Maximum number of pages to scrape.

        Returns:
            List of RawJob objects.
        """
        ...

    @abstractmethod
    def parse_job(self, raw_data: dict | object) -> Optional[RawJob]:
        """Parse a single job from raw source data.

        Args:
            raw_data: Raw job data (dict from API or BeautifulSoup element).

        Returns:
            RawJob if parsed successfully, None otherwise.
        """
        ...

    def scrape_safe(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Scrape with error handling — never raises, returns empty on failure.

        Args:
            keyword: Job search keyword.
            max_pages: Maximum pages to scrape.

        Returns:
            List of RawJob objects (empty on error).
        """
        start_time = time.time()
        try:
            jobs = self.scrape(keyword, max_pages)
            duration_ms = int((time.time() - start_time) * 1000)
            logger.info(
                f"Scraped {len(jobs)} jobs from {self.source.value} for '{keyword}'",
                extra={
                    "source": self.source.value,
                    "job_count": len(jobs),
                    "duration_ms": duration_ms,
                },
            )
            return jobs
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(
                f"Failed to scrape {self.source.value} for '{keyword}': {e}",
                extra={
                    "source": self.source.value,
                    "error_type": type(e).__name__,
                    "duration_ms": duration_ms,
                },
                exc_info=True,
            )
            return []
