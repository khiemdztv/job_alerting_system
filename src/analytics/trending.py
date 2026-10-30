"""Trending job-market summary (skills, titles, companies, salaries, sources).

``trending_summary`` scans the jobs table at most once per 6 hours: the result
is cached in the users table as ``user_id="CACHE", sk="TRENDING#<days>d#<city>"``
with a ``ttl`` attribute.  ``format_trending`` renders it as Telegram HTML.
"""

from __future__ import annotations

import html
import json
import re
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import boto3

from src.common.logger import get_logger
from src.config import get_settings
from src.matcher.search import normalize_location

logger = get_logger(__name__)

CACHE_TTL_SECONDS = 6 * 3600
MAX_MESSAGE_CHARS = 3500
_FIELDS = (
    "title", "company", "location_normalized", "salary_min", "salary_max",
    "tags", "posted_at", "first_seen_at", "source",
)
_SENIORITY = re.compile(
    r"\b(?:senior|junior|sr|jr|lead|principal|staff|intern|internship|fresher|"
    r"entry level|middle|mid|trainee|thực tập sinh|thực tập|cao cấp|trưởng nhóm)\b\.?",
    flags=re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """Lowercase, drop bracketed notes and seniority words, collapse spaces."""
    text = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", str(title or "").lower())
    text = _SENIORITY.sub(" ", text)
    text = re.sub(r"[^\w\s+#./-]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -/.")


def _midpoint(item: dict) -> float | None:
    values = []
    for field in ("salary_min", "salary_max"):
        raw = item.get(field)
        if raw not in (None, "") and float(raw) > 0:
            values.append(float(raw))
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> int | None:
    return int(statistics.median(values)) if values else None


def _scan_window(settings, cutoff_iso: str) -> list[dict]:
    table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
        settings.dynamodb_jobs_table
    )
    names = {f"#{name}": name for name in _FIELDS}
    kwargs = {
        "ProjectionExpression": ", ".join(names),
        "ExpressionAttributeNames": names,
        "FilterExpression": (
            "#posted_at >= :cutoff OR "
            "(attribute_not_exists(#posted_at) AND #first_seen_at >= :cutoff)"
        ),
        "ExpressionAttributeValues": {":cutoff": cutoff_iso},
    }
    items: list[dict] = []
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def compute_summary(items: list[dict], days: int, city: str | None) -> dict:
    tags: Counter = Counter()
    titles: Counter = Counter()
    companies: Counter = Counter()
    cities: Counter = Counter()
    sources: Counter = Counter()
    salaries: list[float] = []
    city_salaries: dict[str, list[float]] = {}
    for item in items:
        for tag in item.get("tags") or []:
            tag = str(tag).strip().lower()
            if tag:
                tags[tag] += 1
        title = normalize_title(item.get("title", ""))
        if title:
            titles[title] += 1
        company = str(item.get("company") or "").strip()
        if company:
            companies[company] += 1
        place = str(item.get("location_normalized") or "").strip()
        if place:
            cities[place] += 1
        if item.get("source"):
            sources[str(item["source"])] += 1
        mid = _midpoint(item)
        if mid is not None:
            salaries.append(mid)
            if place:
                city_salaries.setdefault(place, []).append(mid)
    return {
        "days": days,
        "city": city or "",
        "total_jobs": len(items),
        "top_skills": [list(p) for p in tags.most_common(15)],
        "top_titles": [list(p) for p in titles.most_common(10)],
        "top_companies": [list(p) for p in companies.most_common(10)],
        "median_salary": _median(salaries),
        "salary_sample": len(salaries),
        "top_cities": [
            {"city": name, "jobs": count, "median_salary": _median(city_salaries.get(name, []))}
            for name, count in cities.most_common(5)
        ],
        "sources": [list(p) for p in sources.most_common()],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def trending_summary(days: int = 7, city: str | None = None) -> dict:
    """Return the (cached) trending summary for jobs published in the last ``days``."""
    settings = get_settings()
    city_key = normalize_location(city) if city else ""
    key = {"user_id": "CACHE", "sk": f"TRENDING#{days}d#{city_key or 'all'}"}
    users = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
        settings.dynamodb_users_table
    )
    try:
        cached = users.get_item(Key=key).get("Item")
        if cached and int(cached.get("ttl", 0)) > time.time():
            return json.loads(cached["payload"])
    except Exception as exc:
        logger.warning("Trending cache read failed: %s", type(exc).__name__)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    items = _scan_window(settings, cutoff)
    if city_key:
        items = [i for i in items if i.get("location_normalized") == city_key]
    summary = compute_summary(items, days, city_key or None)
    try:
        users.put_item(Item={
            **key,
            "payload": json.dumps(summary, ensure_ascii=False),
            "ttl": int(time.time()) + CACHE_TTL_SECONDS,
        })
    except Exception as exc:
        logger.warning("Trending cache write failed: %s", type(exc).__name__)
    return summary


def _vnd(value) -> str:
    return f"{value / 1_000_000:.1f} triệu" if value else "n/a"


def format_trending(summary: dict) -> str:
    """Telegram HTML message (<= 3500 chars) for a trending summary."""
    esc = html.escape
    scope = f" · {esc(summary['city'].title())}" if summary.get("city") else ""
    lines = [
        f"📈 <b>Xu hướng tuyển dụng {summary.get('days', 7)} ngày{scope}</b>",
        f"{summary.get('total_jobs', 0)} tin đăng. Lương trung vị: "
        f"<b>{_vnd(summary.get('median_salary'))}</b> "
        f"({summary.get('salary_sample', 0)} tin có lương)",
    ]

    def block(title: str, rows: list[str]) -> None:
        if rows:
            lines.extend(["", f"<b>{title}</b>", *rows])

    block("Kỹ năng hot", [
        f"{i}. {esc(str(name))} ({count})"
        for i, (name, count) in enumerate(summary.get("top_skills", []), 1)
    ])
    block("Vị trí nhiều nhất", [
        f"{i}. {esc(str(name))} ({count})"
        for i, (name, count) in enumerate(summary.get("top_titles", []), 1)
    ])
    block("Công ty tuyển nhiều", [
        f"{i}. {esc(str(name))} ({count})"
        for i, (name, count) in enumerate(summary.get("top_companies", []), 1)
    ])
    block("Thành phố", [
        f"• {esc(str(row['city']).title())}: {row['jobs']} tin, "
        f"lương TV {_vnd(row['median_salary'])}"
        for row in summary.get("top_cities", [])
    ])
    block("Nguồn", [f"• {esc(str(name))}: {count}" for name, count in summary.get("sources", [])])
    text = "\n".join(lines)
    while len(text) > MAX_MESSAGE_CHARS and len(lines) > 3:
        lines.pop()  # drop trailing rows (sources, then cities, ...) until it fits
        text = "\n".join(lines)
    return text[:MAX_MESSAGE_CHARS]
