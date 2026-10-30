"""Fetch job detail pages for thin postings, within a strict per-invocation budget.

Many listing cards carry a one-line description and no deadline.  For those
(description < ``min_description`` chars or no ``valid_through_raw``) the ETL
reads the posting's own detail page once and fills description, requirements,
benefits and the deadline from its ``JobPosting`` JSON-LD (OpenGraph as a
fallback), using :func:`src.scrapers.generic_jsonld_scraper.parse_detail_html`.

Budget (all from ``os.environ``; see :class:`DetailFetchConfig`):

* at most ``VIECLAMBOT_DETAIL_FETCH_PER_BATCH`` pages per ETL invocation (5);
* one request per ``VIECLAMBOT_DETAIL_FETCH_INTERVAL_SECONDS`` (1 s), each with a
  ``VIECLAMBOT_DETAIL_FETCH_TIMEOUT_SECONDS`` timeout (5 s);
* stop after ``VIECLAMBOT_DETAIL_FETCH_BUDGET_SECONDS`` of wall time (12 s) or when
  the Lambda has too little time left;
* never fetch hosts known to block bots (``VIECLAMBOT_DETAIL_FETCH_SKIP_HOSTS``),
  and give up on a host for the rest of the run after a 403/429/503.

A page is fetched once per posting: the ETL stores ``details_fetched_at`` and on
later re-scrapes reuses the stored description/deadline instead of re-fetching.
Disabled by default under pytest; tests inject a fake session.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from src.common.logger import get_logger
from src.common.models import RawJob

logger = get_logger(__name__)

DEFAULT_SKIP_HOSTS = ("jobsgo.vn", "topcv.vn", "linkedin.com", "indeed.com", "glassdoor.com")
_BLOCKED_STATUSES = {403, 429, 503}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_enabled() -> bool:
    value = os.environ.get("VIECLAMBOT_DETAIL_FETCH_ENABLED")
    if value is None:
        # Unit tests must never touch the network (tests/conftest.py blocks it).
        return "PYTEST_CURRENT_TEST" not in os.environ
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class DetailFetchConfig:
    enabled: bool = True
    max_pages: int = 5
    timeout: float = 5.0
    min_interval: float = 1.0
    budget_seconds: float = 12.0
    min_description: int = 200
    # Leave this much Lambda time for transform + DynamoDB + S3 after fetching.
    reserve_ms: int = 8000
    skip_hosts: tuple[str, ...] = DEFAULT_SKIP_HOSTS

    @classmethod
    def from_env(cls) -> "DetailFetchConfig":
        hosts = os.environ.get("VIECLAMBOT_DETAIL_FETCH_SKIP_HOSTS")
        return cls(
            enabled=_env_enabled(),
            max_pages=max(0, _env_int("VIECLAMBOT_DETAIL_FETCH_PER_BATCH", 5)),
            timeout=_env_float("VIECLAMBOT_DETAIL_FETCH_TIMEOUT_SECONDS", 5.0),
            min_interval=_env_float("VIECLAMBOT_DETAIL_FETCH_INTERVAL_SECONDS", 1.0),
            budget_seconds=_env_float("VIECLAMBOT_DETAIL_FETCH_BUDGET_SECONDS", 12.0),
            skip_hosts=(tuple(h.strip().lower() for h in hosts.split(",") if h.strip())
                        if hosts is not None else DEFAULT_SKIP_HOSTS),
        )


def needs_details(raw: RawJob, min_description: int = 200) -> bool:
    """A posting whose card is too thin to classify or to expire on time."""
    return len((raw.description or "").strip()) < min_description or not raw.valid_through_raw


def _host(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"}:
        return ""
    return (parts.hostname or "").lower()


def host_allowed(url: str, skip_hosts: tuple[str, ...] = DEFAULT_SKIP_HOSTS) -> bool:
    host = _host(url)
    if not host:
        return False
    return not any(host == h or host.endswith("." + h) for h in skip_hosts)


def merge_details(raw: RawJob, details: dict[str, Any]) -> RawJob:
    """Copy of ``raw`` with detail-page fields filled where the card was thinner."""
    if not details:
        return raw
    update: dict[str, Any] = {}
    description = str(details.get("description") or "")
    if len(description) > len(raw.description or ""):
        update["description"] = description
    requirements = str(details.get("requirements") or "")
    if len(requirements) > len(raw.requirements or ""):
        update["requirements"] = requirements
    benefits = str(details.get("benefits") or "")
    if benefits and len(benefits) > len(raw.benefits_raw or ""):
        update["benefits_raw"] = benefits
    for name in ("valid_through_raw", "posted_at_raw", "salary_raw", "company", "location",
                 "job_type_raw"):
        value = str(details.get(name) or "").strip()
        if value and not str(getattr(raw, name, "") or "").strip():
            update[name] = value
    return raw.model_copy(update=update) if update else raw


@dataclass
class DetailFetcher:
    """Rate-limited, budgeted detail page fetcher for one ETL invocation."""

    config: DetailFetchConfig = field(default_factory=DetailFetchConfig.from_env)
    session: Any = None
    remaining_ms: Optional[Callable[[], int]] = None  # Lambda context.get_remaining_time_in_millis
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    pages: int = 0
    failures: int = 0
    _started: Optional[float] = None
    _last_request: Optional[float] = None
    _blocked_hosts: set[str] = field(default_factory=set)

    def can_fetch(self, url: str) -> bool:
        return (self.config.enabled and host_allowed(url, self.config.skip_hosts)
                and _host(url) not in self._blocked_hosts)

    def budget_left(self) -> bool:
        if not self.config.enabled or self.pages >= self.config.max_pages:
            return False
        if self._started is not None and (
            self.clock() - self._started + self.config.min_interval + self.config.timeout
            > self.config.budget_seconds
        ):
            return False
        if self.remaining_ms is not None:
            try:
                left = int(self.remaining_ms())
            except Exception:  # noqa: BLE001 - a broken context must not stop the ETL
                return True
            if left < self.config.reserve_ms + self.config.timeout * 1000:
                return False
        return True

    def _session(self):
        if self.session is None:
            import requests

            from src.config import get_settings

            self.session = requests.Session()
            self.session.headers.update({
                "User-Agent": get_settings().user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
            })
        return self.session

    def fetch(self, url: str) -> Optional[dict[str, Any]]:
        """Parsed detail fields, ``{}`` for a page without job data, None on failure."""
        if not self.can_fetch(url) or not self.budget_left():
            return None
        from src.scrapers.generic_jsonld_scraper import parse_detail_html

        now = self.clock()
        if self._started is None:
            self._started = now
        if self._last_request is not None:
            wait = self.config.min_interval - (now - self._last_request)
            if wait > 0:
                self.sleep(wait)
        self._last_request = self.clock()
        self.pages += 1
        host = _host(url)
        try:
            response = self._session().get(url, timeout=self.config.timeout,
                                           allow_redirects=True)
        except Exception as exc:  # noqa: BLE001 - network trouble never fails the ETL
            self.failures += 1
            logger.info(f"Detail fetch failed for {url}: {type(exc).__name__}")
            return None
        status = int(getattr(response, "status_code", 0) or 0)
        if status in _BLOCKED_STATUSES:
            self._blocked_hosts.add(host)
            self.failures += 1
            logger.warning(f"Detail fetch blocked ({status}) by {host}; skipping it this run")
            return None
        if status != 200:
            self.failures += 1
            return None
        content = getattr(response, "content", b"") or b""
        text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) \
            else str(getattr(response, "text", "") or "")
        try:
            return parse_detail_html(text, str(getattr(response, "url", "") or url))
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            logger.warning(f"Detail page parse failed for {url}: {exc}")
            return None


def stored_as_details(item: dict[str, Any]) -> dict[str, Any]:
    """A stored DynamoDB job item in ``parse_detail_html`` shape (for re-scrapes)."""
    return {
        "description": item.get("description") or "",
        "requirements": item.get("requirements") or "",
        "valid_through_raw": item.get("expires_at") or "",
    }


def fill_details(
    raw_jobs: list[RawJob],
    job_ids: list[str],
    fetcher: DetailFetcher,
    lookup: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    now: Optional[datetime] = None,
) -> tuple[list[RawJob], dict[int, dict[str, Any]], dict[str, int]]:
    """Fill thin postings from stored details or a fresh detail fetch.

    Returns ``(raw_jobs, info, stats)`` where ``info[i]`` holds
    ``details_fetched_at`` and stored ``benefits`` for each improved posting.
    ``lookup(job_id)`` returns the stored item (or None) so a posting already
    fetched on an earlier run is not fetched again.
    """
    now = now or datetime.now(timezone.utc)
    result = list(raw_jobs)
    info: dict[int, dict[str, Any]] = {}
    stats = {"candidates": 0, "reused": 0, "fetched": 0, "failed": 0, "skipped": 0}
    for index, raw in enumerate(raw_jobs):
        if not needs_details(raw, fetcher.config.min_description):
            continue
        if not fetcher.can_fetch(raw.source_url):
            stats["skipped"] += 1
            continue
        stats["candidates"] += 1
        stored = None
        if lookup is not None and index < len(job_ids) and job_ids[index]:
            try:
                stored = lookup(job_ids[index])
            except Exception as exc:  # noqa: BLE001 - a read failure only costs a fetch
                logger.info(f"Stored detail lookup failed: {exc}")
        if stored and stored.get("details_fetched_at"):
            result[index] = merge_details(raw, stored_as_details(stored))
            info[index] = {"details_fetched_at": stored["details_fetched_at"],
                           "benefits": list(stored.get("benefits") or [])}
            stats["reused"] += 1
            continue
        if not fetcher.budget_left():
            continue
        details = fetcher.fetch(raw.source_url)
        if details is None:
            stats["failed"] += 1
            continue
        result[index] = merge_details(raw, details)
        info[index] = {"details_fetched_at": now.isoformat(), "benefits": []}
        stats["fetched"] += 1
    return result, info, stats
