"""Read currently active listings directly from reliable Vietnamese job boards."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.common.logger import get_logger
from src.common.text_utils import clean_vn_text
from src.etl.transformer import Transformer
from src.matcher.search import identity, rank_jobs
from src.scrapers.careerviet_scraper import CareerVietScraper
from src.scrapers.jobsgo_scraper import JobsGoScraper
from src.scrapers.topdev_scraper import TopDevScraper
from src.scrapers.vieclam24h_scraper import ViecLam24hScraper
from src.scrapers.vietnamworks_scraper import VietnamWorksScraper
from src.web_search.you_search import _canonical_url

logger = get_logger(__name__)


def _query_variants(query: str) -> list[str]:
    variants = [re.sub(r"\s+", " ", query).strip()]
    cleaned = clean_vn_text(query)
    if "intern" in cleaned and re.search(r"(?<!\w)ai(?!\w)", cleaned):
        variants.extend(["ai intern", "machine learning intern"])
    return list(dict.fromkeys(item for item in variants if item))


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


def _to_live_job(raw_job, transformer: Transformer) -> dict | None:
    transformed = transformer.transform(raw_job)
    if transformed is None:
        return None
    job = transformed.model_dump(mode="json")
    job["source_url"] = _canonical_url(job.get("source_url", ""))
    label = {
        "careerviet": "CareerViet",
        "jobsgo": "JobsGO",
        "topdev": "TopDev",
        "vieclam24h": "ViecLam24h",
        "vietnamworks": "VietnamWorks",
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
    internship_only = _is_internship_query(query)
    broad_query = _board_query(query)
    searches = [
        (ViecLam24hScraper(), query),
        (CareerVietScraper(), broad_query),
        (JobsGoScraper(), broad_query),
        (VietnamWorksScraper(), broad_query),
        (TopDevScraper(internship_only=internship_only), _topdev_query(query)),
    ]
    raw_jobs = []
    with ThreadPoolExecutor(max_workers=len(searches)) as executor:
        futures = {
            executor.submit(scraper.scrape, search_query, 1): scraper
            for scraper, search_query in searches
            if deadline_at is None or time.monotonic() < deadline_at - 5
        }
        for future in as_completed(futures):
            scraper = futures[future]
            try:
                raw_jobs.extend(future.result())
            except Exception:
                logger.exception("Live-board search failed for %s", scraper.source.value)

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
