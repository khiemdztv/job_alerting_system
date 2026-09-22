"""Read currently active listings directly from reliable Vietnamese job boards."""

from __future__ import annotations

import re
import time

from src.common.logger import get_logger
from src.common.text_utils import clean_vn_text
from src.etl.transformer import Transformer
from src.matcher.search import identity, rank_jobs
from src.scrapers.careerviet_scraper import CareerVietScraper
from src.scrapers.vieclam24h_scraper import ViecLam24hScraper
from src.web_search.you_search import _canonical_url

logger = get_logger(__name__)


def _query_variants(query: str) -> list[str]:
    variants = [re.sub(r"\s+", " ", query).strip()]
    cleaned = clean_vn_text(query)
    if "intern" in cleaned and re.search(r"(?<!\w)ai(?!\w)", cleaned):
        variants.extend(["ai intern", "machine learning intern"])
    return list(dict.fromkeys(item for item in variants if item))


def _to_live_job(raw_job, transformer: Transformer) -> dict | None:
    transformed = transformer.transform(raw_job)
    if transformed is None:
        return None
    job = transformed.model_dump(mode="json")
    job["source_url"] = _canonical_url(job.get("source_url", ""))
    label = {
        "careerviet": "CareerViet",
        "vieclam24h": "ViecLam24h",
    }.get(str(job.get("source", "")), str(job.get("source", "")))
    job["source"] = f"Live · {label}"
    job["job_id"] = "live:" + identity(job)
    job["is_web_result"] = True
    job["is_live_listing"] = True
    return job


def search_live_job_boards(
    query: str,
    *,
    location: str | None = None,
    limit: int = 20,
    deadline_at: float | None = None,
) -> list[dict]:
    """Search active listing pages when the search provider index is incomplete."""
    variants = _query_variants(query)
    raw_jobs = []
    searches = [
        (ViecLam24hScraper(), variants[:1]),
        (CareerVietScraper(), variants[:2]),
    ]
    for scraper, scraper_queries in searches:
        for search_query in scraper_queries:
            if deadline_at is not None and time.monotonic() >= deadline_at - 5:
                logger.warning("Live-board search stopped at request deadline")
                break
            try:
                raw_jobs.extend(scraper.scrape(search_query, max_pages=1))
            except Exception:
                logger.exception(
                    "Live-board search failed for %s", scraper.source.value
                )

    transformer = Transformer()
    jobs = [job for raw in raw_jobs if (job := _to_live_job(raw, transformer))]
    ranked, seen = [], set()
    for variant in variants:
        for job in rank_jobs(jobs, variant, location=location, limit=None):
            key = identity(job)
            if key in seen:
                continue
            seen.add(key)
            ranked.append(job)
            if len(ranked) >= limit:
                return ranked
    logger.info("Live-board search normalized %s/%s jobs", len(ranked), len(raw_jobs))
    return ranked
