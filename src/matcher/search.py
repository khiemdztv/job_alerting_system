"""Shared, accent-insensitive relevance and filters for search and alerts."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from src.common.text_utils import clean_vn_text
from src.matcher.synonyms import expand_token, tokenize_query


def contains(text: str, term: str) -> bool:
    return bool(term and re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text))


def timestamp(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return None


@lru_cache(maxsize=512)
def concepts(query: str) -> tuple[tuple[str, ...], ...]:
    query = clean_vn_text(query)
    # These describe the act of searching, not a required skill or seniority.
    for filler in (
        "toi muon tim",
        "tim viec lam",
        "tim viec",
        "viec lam",
        "tuyen dung",
        "nhan vien",
        "chuyen vien",
        "ung tuyen",
        "tim job",
    ):
        query = re.sub(r"(?<!\w)" + filler + r"(?!\w)", " ", query)
    return tuple(tuple(expand_token(t)) for t in tokenize_query(query))


@lru_cache(maxsize=2048)
def _search_fields(title, company, search_blob, description, requirements, tags):
    title = clean_vn_text(title)
    blob = clean_vn_text(
        " ".join(
            str(v)
            for v in (
                title,
                company,
                search_blob,
                description,
                requirements,
                " ".join(tags),
            )
            if v
        )
    )
    return title, blob


def relevance(job: dict, query: str) -> int:
    groups = concepts(query)
    if not groups:
        return 0
    raw_title = job.get("title") or job.get("title_normalized", "")
    search_blob = str(job.get("search_blob") or "")
    if search_blob:
        # ETL already stores search_blob as normalized text. Cleaning several
        # kilobytes again for every job made a webhook search exceed 30 seconds.
        title = clean_vn_text(raw_title)
        blob = " ".join(
            value
            for value in (
                title,
                clean_vn_text(job.get("company", "")),
                search_blob.lower(),
                clean_vn_text(" ".join(job.get("tags") or [])),
            )
            if value
        )
    else:
        title, blob = _search_fields(
            raw_title,
            job.get("company", ""),
            "",
            job.get("description", ""),
            job.get("requirements", ""),
            tuple(job.get("tags") or []),
        )
    title_hits = [any(contains(title, term) for term in group) for group in groups]
    if not all(any(contains(blob, term) for term in group) for group in groups):
        return 0
    if contains(title, clean_vn_text(query)):
        return 100
    if all(title_hits):
        return 80
    return 40 + 10 * sum(title_hits)


@lru_cache(maxsize=8192)
def normalize_location(value: str) -> str:
    from src.etl.transformer import LOCATION_MAP

    text = clean_vn_text(value)
    for alias, canonical in sorted(LOCATION_MAP.items(), key=lambda p: -len(p[0])):
        text = re.sub(
            r"(?<!\w)" + re.escape(clean_vn_text(alias)) + r"(?!\w)", clean_vn_text(canonical), text
        )
    return text


def matches_filters(
    job: dict,
    location: str | None = None,
    salary_min: int | None = None,
    since: datetime | None = None,
    max_age_days: int = 60,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    expiry = timestamp(job.get("expires_at"))
    if expiry and expiry <= now:
        return False
    if job.get("ttl") is not None and int(job["ttl"]) <= now.timestamp():
        return False
    scraped = timestamp(job.get("scraped_at"))
    if scraped and scraped < now - timedelta(days=max_age_days):
        return False
    if since and (not scraped or scraped < since):
        return False
    if location and not contains(
        normalize_location(f"{job.get('location', '')} {job.get('location_normalized', '')}"),
        normalize_location(location),
    ):
        return False
    salary = job.get("salary_max") or job.get("salary_min")
    if salary_min and salary is not None and int(salary) < salary_min:
        return False
    return True


def identity(job: dict) -> str:
    """Deduplicate mirrors, but never merge anonymous employers or different cities."""
    company = clean_vn_text(job.get("company", ""))
    if company and company not in {"n/a", "unknown", "ybox employer"}:
        key = "|".join(
            (
                clean_vn_text(job.get("title", "")),
                company,
                normalize_location(job.get("location", "")),
            )
        )
    else:
        key = job.get("source_url") or job.get("job_id") or repr(job)
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def rank_jobs(
    jobs: list[dict],
    query: str,
    *,
    location=None,
    salary_min=None,
    since=None,
    max_age_days=60,
    limit: int | None = 20,
) -> list[dict]:
    now = datetime.now(timezone.utc)
    ranked = []
    for job in jobs:
        if not matches_filters(job, location, salary_min, since, max_age_days, now):
            continue
        score = relevance(job, query)
        if score:
            posted = timestamp(job.get("posted_at"))
            observed = timestamp(job.get("scraped_at")) or datetime.min.replace(tzinfo=timezone.utc)
            # Some sources accidentally expose the closing date as date posted.
            recent = posted if posted and posted <= now else observed
            ranked.append((score, recent, job))
    ranked.sort(key=lambda r: (r[0], r[1], r[2].get("job_id", "")), reverse=True)
    tiers, seen = defaultdict(lambda: defaultdict(deque)), set()
    for score, _, job in ranked:
        key = identity(job)
        if key not in seen:
            seen.add(key)
            tiers[score][job.get("source", "unknown")].append(job)
    result = []
    # Diversity only breaks ties in relevance; weak hits never outrank exact hits.
    for sources in tiers.values():
        while any(sources.values()):
            for queue in sources.values():
                if queue:
                    result.append(queue.popleft())
                    if limit is not None and len(result) >= limit:
                        return result
    return result


def parse_search(text: str) -> tuple[str, str | None]:
    text = text.strip()
    for separator in ("|", ","):
        if separator in text:
            keyword, location = text.split(separator, 1)
            return keyword.strip(), location.strip() or None
    parts = re.split(r"\s+(?:tại|ở|tai|o)\s+", text, maxsplit=1, flags=re.I)
    return parts[0].strip(), parts[1].strip() if len(parts) > 1 else None
