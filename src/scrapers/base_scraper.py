"""
Abstract base scraper for ViecLamBot.

All source-specific scrapers inherit from this base class,
ensuring a consistent interface and shared functionality.
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import urlparse

import requests

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.config import get_settings

logger = get_logger(__name__)

# Pool of realistic User-Agent strings for anti-blocking rotation
_USER_AGENT_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


# Responses that mean the site refused us (anti-bot, rate limit), not a parser bug.
BLOCKED_STATUSES = {403, 429, 503}


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
        self.deadline_at: float | None = None
        # Seconds that must remain before deadline_at for a request to start.
        # Batch runs keep a wide margin so S3/SQS persistence still fits.
        self.budget_margin: float = 20.0
        self.request_timeout: float = 5.0
        self.live_mode: bool = False
        self.last_error: str = ""

    def _create_session(self) -> requests.Session:
        """Create a requests session with randomized headers and retry config."""
        session = requests.Session()

        # Randomize User-Agent per session to avoid fingerprinting
        headers = dict(self.settings.scraper_headers)
        headers["User-Agent"] = random.choice(_USER_AGENT_POOL)
        session.headers.update(headers)

        # Retry adapter
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.adapters.Retry(
                total=1,
                backoff_factor=1.0,
                status_forcelist=[429, 500, 502, 503, 504],
            )
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    def use_live_budget(self, deadline_at: float | None, timeout: float) -> None:
        """Tune this scraper for an interactive request instead of a batch run.

        A Telegram webhook has roughly 18 seconds for everything, so each board
        gets one short request, no automatic retry, and no inter-request delay.
        """
        self.live_mode = True
        self.deadline_at = deadline_at
        self.budget_margin = 1.0
        self.request_timeout = timeout
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _rate_limit(self) -> None:
        """Enforce delay between requests with jitter to avoid fingerprinting."""
        if (
            self.deadline_at is not None
            and time.monotonic() + self.budget_margin >= self.deadline_at
        ):
            raise TimeoutError("Scrape budget exhausted")
        if self.live_mode:
            self._last_request_time = time.time()
            return
        elapsed = time.time() - self._last_request_time
        base_delay = self.settings.scrape_delay_seconds
        # Add random jitter (0.5-1.5s) to avoid consistent timing patterns
        jitter = random.uniform(0.5, 1.5)
        target_delay = base_delay + jitter
        if elapsed < target_delay:
            sleep_time = target_delay - elapsed
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

        # Add Referer header (site homepage) to appear more browser-like
        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        headers = kwargs.pop("headers", {})
        headers.setdefault("Referer", referer)

        logger.info(
            f"Scraping {url}",
            extra={"source": self.source.value},
        )
        try:
            timeout = kwargs.pop("timeout", self.request_timeout)
            if self.live_mode:
                timeout = min(float(timeout), self.request_timeout)
            response = self.session.get(
                url, params=params, timeout=timeout, headers=headers, **kwargs
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            self.last_error = self._error_label(exc)
            raise

    @staticmethod
    def _error_label(exc: requests.RequestException) -> str:
        """Name the failure so health reports can tell "blocked" from "broken"."""
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status in BLOCKED_STATUSES:
            return f"HTTP{status}"
        return type(exc).__name__

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
            f"POST {urlparse(url).netloc}",
            extra={"source": self.source.value},
        )
        try:
            timeout = kwargs.pop("timeout", self.request_timeout)
            response = self.session.post(url, json=json_data, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            self.last_error = self._error_label(exc)
            raise

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
        self.last_error = ""
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
            self.last_error = type(e).__name__
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
