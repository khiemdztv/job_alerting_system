"""Read currently active listings directly from every registered job board.

Each interactive search fans out to all live-capable boards in parallel, with
one short request per board and a hard deadline, then normalizes and ranks the
union.  Results are cached briefly per warm container so repeated searches do
not hammer the boards.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from src.common.logger import get_logger
from src.common.models import JobSource
from src.common.text_utils import clean_vn_text
from src.config import Settings, get_settings
from src.etl.transformer import Transformer
from src.matcher.search import identity, normalize_location, query_variants, rank_jobs_multi
from src.scrapers.registry import SourceSpec, live_sources, source_label
from src.web_search.you_search import _canonical_url

logger = get_logger(__name__)

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}
_last_stats: dict[str, dict] = {}


def _is_internship_query(query: str) -> bool:
    cleaned = clean_vn_text(query)
    return bool(re.search(r"(?<!\w)(?:intern|internship|thuc tap)(?!\w)", cleaned))


def _board_query(query: str) -> str:
    """Broaden only seniority wording while retaining the requested field."""
    cleaned = clean_vn_text(query)
    if "intern" in cleaned and re.search(r"(?<!\w)ai(?!\w)", cleaned):
        return "ai intern"
    return re.sub(r"\s+", " ", query).strip()


def _topdev_query(query: str) -> str:
    cleaned = clean_vn_text(query)
    if "intern" in cleaned and re.search(r"(?<!\w)ai(?!\w)", cleaned):
        return "ai"
    topic = re.sub(r"(?<!\w)(?:intern|internship)(?!\w)", " ", query, flags=re.I)
    return re.sub(r"\s+", " ", topic).strip() or query


def _query_for(spec: SourceSpec, query: str) -> str:
    if spec.source is JobSource.TOPDEV:
        return _topdev_query(query)
    if spec.source is JobSource.VIECLAM24H:
        return re.sub(r"\s+", " ", query).strip()
    return _board_query(query)


def _to_live_job(raw_job, transformer: Transformer) -> dict | None:
    transformed = transformer.transform(raw_job)
    if transformed is None:
        return None
    job = transformed.model_dump(mode="json")
    job["source_url"] = _canonical_url(job.get("source_url", ""))
    job["source"] = f"Live · {source_label(str(job.get('source', '')))}"[:30]
    job["job_id"] = "live:" + identity(job)
    job["is_web_result"] = True
    job["is_live_listing"] = True
    return job


def _scrape_board(
    spec: SourceSpec,
    query: str,
    *,
    internship_only: bool,
    deadline_at: float | None,
    settings: Settings,
) -> tuple[SourceSpec, list, str, int]:
    started = time.monotonic()
    scraper = spec.factory()
    timeout = spec.live_timeout or settings.live_search_timeout_seconds
    if deadline_at is not None:
        timeout = max(1.0, min(timeout, deadline_at - time.monotonic() - 0.5))
    scraper.use_live_budget(deadline_at, timeout)
    if spec.source is JobSource.TOPDEV:
        scraper.internship_only = internship_only
    try:
        jobs = scraper.scrape(query, 1)
        error = scraper.last_error
    except Exception as exc:  # one slow or changed board must not hide the others
        jobs, error = [], type(exc).__name__
        logger.warning("Live-board search failed for %s: %s", spec.source.value, error)
    finally:
        scraper.session.close()
    return spec, jobs, error, int((time.monotonic() - started) * 1000)


def last_live_stats() -> dict[str, dict]:
    """Per-source outcome of the most recent live search in this process."""
    return dict(_last_stats)


def clear_live_cache() -> None:
    with _cache_lock:
        _cache.clear()


def search_live_job_boards(
    query: str,
    *,
    location: str | None = None,
    limit: int = 20,
    deadline_at: float | None = None,
    settings: Settings | None = None,
    stats: dict[str, dict] | None = None,
) -> list[dict]:
    """Search active listing pages of every live board, in parallel, within a deadline."""
    settings = settings or get_settings()
    specs = live_sources(settings)
    if not specs or not query.strip():
        return []
    cache_key = (clean_vn_text(query), normalize_location(location or ""), limit)
    with _cache_lock:
        cached = _cache.get(cache_key)
    if cached and time.monotonic() - cached[0] <= settings.live_search_cache_seconds:
        logger.info("Live-board search served from cache: %s jobs", len(cached[1]))
        if stats is not None:
            stats.update(_last_stats)
        return list(cached[1])

    internship_only = _is_internship_query(query)
    executor = ThreadPoolExecutor(max_workers=len(specs))
    futures: dict[Future, SourceSpec] = {}
    for spec in specs:
        if deadline_at is not None and time.monotonic() >= deadline_at - 1.5:
            logger.warning("Live-board search skipped %s: no budget left", spec.source.value)
            continue
        future = executor.submit(
            _scrape_board,
            spec,
            _query_for(spec, query),
            internship_only=internship_only,
            deadline_at=deadline_at,
            settings=settings,
        )
        futures[future] = spec

    raw_jobs, outcomes = [], {}
    pending = set(futures)
    try:
        while pending:
            remaining = None
            if deadline_at is not None:
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                spec, jobs, error, duration_ms = future.result()
                raw_jobs.extend(jobs)
                outcomes[spec.source.value] = {
                    "jobs": len(jobs), "error": error, "duration_ms": duration_ms,
                }
    finally:
        for future in pending:
            outcomes[futures[future].source.value] = {
                "jobs": 0, "error": "DeadlineExceeded", "duration_ms": None,
            }
        # Never block the webhook on a board that is still responding.
        executor.shutdown(wait=False, cancel_futures=True)

    transformer = Transformer()
    jobs = [job for raw in raw_jobs if (job := _to_live_job(raw, transformer))]
    relevant = rank_jobs_multi(jobs, query_variants(query), location=location, limit=None)
    labels = {source_label(source): source for source in outcomes}
    for job in relevant:
        source = labels.get(str(job.get("source", "")).removeprefix("Live · "))
        if source:
            outcomes[source]["relevant"] = outcomes[source].get("relevant", 0) + 1
    ranked = relevant[:limit]
    logger.info(
        "Live-board search normalized %s/%s jobs (%s shown) from %s boards: %s",
        len(relevant), len(raw_jobs), len(ranked), len(outcomes), outcomes,
    )
    _last_stats.clear()
    _last_stats.update(outcomes)
    if stats is not None:
        stats.update(outcomes)
    if ranked or not pending:
        with _cache_lock:
            _cache[cache_key] = (time.monotonic(), list(ranked))
    return ranked
