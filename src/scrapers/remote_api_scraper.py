"""Keyless remote-job APIs: Himalayas, Remotive, Arbeitnow and Jobicy.

Terms respected here: every posting keeps the API's original URL, company and
description carry a "via <API>" attribution, Remotive is called at most once
per process lifetime (their cap is 4 calls a day) and never in live mode, and
Jobicy at most once per hour.  Results are filtered locally by keyword.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from src.common.models import JobSource, RawJob
from src.common.text_utils import clean_vn_text
from src.scrapers.ats_scraper import MAX_DESCRIPTION, html_to_text, keyword_matches
from src.scrapers.base_scraper import BaseScraper

HIMALAYAS_URL = "https://himalayas.app/jobs/api/search"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs"

REMOTE_LOCATION = "Remote (toàn cầu)"
REMOTIVE_TTL = 6 * 3600  # cap is 4 calls a day; one call per process in practice
JOBICY_TTL = 3600
ARBEITNOW_TTL = 1800
MAX_PAGES_DEFAULT = 2

# candidate_required_location / jobGeo values that include a person in Vietnam.
_OPEN_TO_VN = re.compile(r"anywhere|worldwide|global|vietnam|viet nam|apac|asia|\bsea\b|^$")

# name -> (fetched_at, jobs); shared by all instances, lives as long as the process.
_CACHE: dict[str, tuple[float, list[RawJob]]] = {}


def _cached(name: str, ttl: float, fetch: Callable[[], list[RawJob]]) -> list[RawJob]:
    hit = _CACHE.get(name)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    jobs = fetch()
    _CACHE[name] = (time.time(), jobs)
    return jobs


def _build(api, title, company, description, url, posted="", job_type="") -> Optional[RawJob]:
    title = str(title or "").strip()
    if not title or not url:
        return None
    body = html_to_text(description or "", MAX_DESCRIPTION - 80)
    return RawJob(
        title=title,
        company=f"{str(company or '').strip()} (via {api})".strip(),
        location=REMOTE_LOCATION,
        description=f"{body}\n\n(via {api})".strip(),
        job_type_raw="remote" + (f", {job_type}" if job_type else ""),
        source=JobSource.REMOTE_API,
        source_url=url,
        posted_at_raw=posted or "",
    )


def _iso_from_seconds(value) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _opens_to_vietnam(geo: str) -> bool:
    return bool(_OPEN_TO_VN.search(clean_vn_text(geo or "")))


class RemoteApiScraper(BaseScraper):
    def __init__(self):
        super().__init__(source=JobSource.REMOTE_API)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        if self.live_mode:
            return []  # call-capped, daily-ish data: batch only
        return self._filtered(keyword, max_pages)

    def scrape_latest(self, max_pages: Optional[int] = None) -> list[RawJob]:
        if self.live_mode:
            return []
        return self._filtered("", max_pages)

    def parse_job(self, raw_data: dict) -> Optional[RawJob]:
        """Dispatch on the ``_api`` marker added by the fetchers."""
        parser = {
            "Himalayas": self._himalayas_job,
            "Remotive": self._remotive_job,
            "Arbeitnow": self._arbeitnow_job,
            "Jobicy": self._jobicy_job,
        }.get(raw_data.get("_api", ""))
        return parser(raw_data) if parser else None

    # ---- orchestration ----------------------------------------------------

    def _filtered(self, keyword: str, max_pages: Optional[int]) -> list[RawJob]:
        pages = min(max_pages or MAX_PAGES_DEFAULT, MAX_PAGES_DEFAULT)
        sources = (
            lambda: self._himalayas(keyword, pages),
            self._remotive,
            lambda: self._arbeitnow(pages),
            self._jobicy,
        )
        jobs: list[RawJob] = []
        for fetch in sources:
            try:
                jobs.extend(fetch())
            except TimeoutError:
                self.last_error = self.last_error or "TimeoutError"
                break
            except Exception:  # one API down must not hide the others
                continue
        seen: set[str] = set()
        out = []
        for job in jobs:
            if job.source_url in seen:
                continue
            seen.add(job.source_url)
            if keyword_matches(keyword, job.title, job.description):
                out.append(job)
        return out

    # ---- Himalayas (searched server-side) ---------------------------------

    def _himalayas(self, keyword: str, pages: int) -> list[RawJob]:
        jobs: list[RawJob] = []
        for page in range(1, pages + 1):
            params = {"q": keyword, "country": "VN", "page": page}
            data = self._get(HIMALAYAS_URL, params=params).json() or {}
            batch = data.get("jobs") or []
            for raw in batch:
                raw["_api"] = "Himalayas"
                job = self.parse_job(raw)
                if job:
                    jobs.append(job)
            if len(batch) < 20:
                break
        return jobs

    def _himalayas_job(self, raw: dict) -> Optional[RawJob]:
        return _build(
            "Himalayas", raw.get("title"), raw.get("companyName"),
            raw.get("description") or raw.get("excerpt"),
            raw.get("applicationLink") or raw.get("guid"),
            _iso_from_seconds(raw.get("pubDate")), raw.get("employmentType") or "",
        )

    # ---- Remotive (cap 4 calls/day: one per process, never live) ----------

    def _remotive(self) -> list[RawJob]:
        def fetch() -> list[RawJob]:
            data = self._get(REMOTIVE_URL, params={"search": ""}).json() or {}
            out = []
            for raw in data.get("jobs") or []:
                raw["_api"] = "Remotive"
                if not _opens_to_vietnam(raw.get("candidate_required_location", "")):
                    continue
                job = self.parse_job(raw)
                if job:
                    out.append(job)
            return out

        return _cached("remotive", REMOTIVE_TTL, fetch)

    def _remotive_job(self, raw: dict) -> Optional[RawJob]:
        return _build(
            "Remotive", raw.get("title"), raw.get("company_name"), raw.get("description"),
            raw.get("url"), raw.get("publication_date") or "",
            str(raw.get("job_type") or "").replace("_", "-"),
        )

    # ---- Arbeitnow (mostly German; keep only remote postings) -------------

    def _arbeitnow(self, pages: int) -> list[RawJob]:
        def fetch() -> list[RawJob]:
            out = []
            for page in range(1, pages + 1):
                data = self._get(ARBEITNOW_URL, params={"page": page}).json() or {}
                batch = data.get("data") or []
                for raw in batch:
                    raw["_api"] = "Arbeitnow"
                    if not raw.get("remote"):
                        continue
                    job = self.parse_job(raw)
                    if job:
                        out.append(job)
                if not batch:
                    break
            return out

        return _cached("arbeitnow", ARBEITNOW_TTL, fetch)

    def _arbeitnow_job(self, raw: dict) -> Optional[RawJob]:
        return _build(
            "Arbeitnow", raw.get("title"), raw.get("company_name"), raw.get("description"),
            raw.get("url"), _iso_from_seconds(raw.get("created_at")),
            ", ".join(raw.get("job_types") or []),
        )

    # ---- Jobicy (cap 1 call/hour: fetch the feed once, filter locally) ----

    def _jobicy(self) -> list[RawJob]:
        def fetch() -> list[RawJob]:
            data = self._get(JOBICY_URL, params={"count": 100}).json() or {}
            out = []
            for raw in data.get("jobs") or []:
                raw["_api"] = "Jobicy"
                if not _opens_to_vietnam(raw.get("jobGeo", "")):
                    continue
                job = self.parse_job(raw)
                if job:
                    out.append(job)
            return out

        return _cached("jobicy", JOBICY_TTL, fetch)

    def _jobicy_job(self, raw: dict) -> Optional[RawJob]:
        return _build(
            "Jobicy", raw.get("jobTitle"), raw.get("companyName"),
            raw.get("jobDescription") or raw.get("jobExcerpt"),
            raw.get("url"), raw.get("pubDate") or "", ", ".join(raw.get("jobType") or []),
        )
