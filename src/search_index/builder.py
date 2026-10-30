"""Build the search index from DynamoDB and upload it to S3.

One Scan (projection = ``SEARCH_FIELDS`` + :data:`EXTRA_FIELDS`) feeds either

* ``index/jobs.sqlite.gz``: an SQLite file with a plain ``jobs`` table (the job
  dict as zlib-compressed JSON plus a recency key) and a contentless FTS5 table
  ``jobs_fts`` over ``title``, ``skills`` (skills + tags), ``company`` and
  ``search_blob``, with the ``unicode61 remove_diacritics 2`` tokenizer.  Text is
  accent-folded with ``clean_vn_text`` before indexing because unicode61 keeps
  "đ" distinct from "d".
* ``index/jobs.pickle.gz``: when this runtime's sqlite3 lacks FTS5, a pure-Python
  inverted index (token -> job positions) pickled with plain builtins only.

Both objects carry S3 metadata ``built_at``, ``scan_started_at``, ``job_count``,
``format`` and ``schema``.  Expired postings are dropped at build time.
"""

from __future__ import annotations

import gzip
import json
import os
import pickle
import shutil
import sqlite3
import tempfile
import time
import zlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import boto3
from botocore.exceptions import ClientError

from src.common.logger import get_logger
from src.search_index.common import (
    EXTRA_FIELDS,
    FORMAT_PICKLE,
    FORMAT_SQLITE,
    FTS_COLUMNS,
    FTS_TOKENIZER,
    SCHEMA_VERSION,
    env_int,
    fold_text,
    fts5_available,
    index_format_setting,
    index_key,
    json_safe,
    parse_time,
    sqlite_info,
)

logger = get_logger(__name__)


@dataclass
class BuildResult:
    path: Path
    format: str
    job_count: int
    built_at: datetime
    scan_started_at: datetime

    def metadata(self) -> dict[str, str]:
        return {
            "built_at": self.built_at.isoformat(),
            "scan_started_at": self.scan_started_at.isoformat(),
            "job_count": str(self.job_count),
            "format": self.format,
            "schema": SCHEMA_VERSION,
        }


def index_fields() -> tuple[str, ...]:
    from src.etl.loader import SEARCH_FIELDS

    return tuple(dict.fromkeys(SEARCH_FIELDS + EXTRA_FIELDS))


def scan_items(table) -> Iterator[dict]:
    """Yield every job item with the search projection, page by page."""
    fields = index_fields()
    names = {f"#f{i}": field for i, field in enumerate(fields)}
    kwargs = {"ProjectionExpression": ", ".join(names), "ExpressionAttributeNames": names}
    while True:
        response = table.scan(**kwargs)
        yield from response.get("Items", [])
        if not response.get("LastEvaluatedKey"):
            return
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def _expired(job: dict, now: datetime) -> bool:
    expiry = parse_time(job.get("expires_at"))
    if expiry and expiry <= now:
        return True
    try:
        return job.get("ttl") is not None and int(job["ttl"]) <= now.timestamp()
    except (TypeError, ValueError):
        return False


def _recency(job: dict, now: datetime) -> float:
    """Publication time as epoch seconds, the tie-breaker rank_jobs uses."""
    from src.matcher.search import published_at

    published = published_at(job, now)
    return published.timestamp() if published else 0.0


def _documents(items: Iterable[dict], now: datetime) -> Iterator[tuple[dict, dict, float]]:
    """(json-safe job, folded column texts, recency) for every live, unique job."""
    seen: set[str] = set()
    fields = index_fields()
    for item in items:
        # Same projection as the DynamoDB scan, also for corpus/eval items.
        job = json_safe({k: item[k] for k in fields if k in item})
        job_id = str(job.get("job_id") or "")
        if not job_id or job_id in seen or _expired(job, now):
            continue
        seen.add(job_id)
        columns = {
            # relevance() reads ``title`` and falls back to ``title_normalized``.
            "title": fold_text(job.get("title") or job.get("title_normalized")),
            "skills": fold_text(job.get("skills"), job.get("tags")),
            "company": fold_text(job.get("company")),
            "search_blob": fold_text(job.get("search_blob")),
        }
        yield job, columns, _recency(job, now)


def _write_sqlite(documents, path: Path, meta: dict) -> int:
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            "PRAGMA journal_mode = OFF;"
            "PRAGMA synchronous = OFF;"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,"
            " recent REAL NOT NULL, data BLOB NOT NULL);"
            f"CREATE VIRTUAL TABLE jobs_fts USING fts5({', '.join(FTS_COLUMNS)},"
            f" content='', tokenize='{FTS_TOKENIZER}');"
        )
        count = 0
        batch_jobs: list = []
        batch_fts: list = []
        for job, columns, recent in documents:
            count += 1
            data = zlib.compress(json.dumps(job, ensure_ascii=False).encode("utf-8"), 6)
            batch_jobs.append((count, job["job_id"], recent, data))
            batch_fts.append((count, *(columns[c] for c in FTS_COLUMNS)))
            if len(batch_jobs) >= 1000:
                _flush(connection, batch_jobs, batch_fts)
        _flush(connection, batch_jobs, batch_fts)
        connection.execute("INSERT INTO jobs_fts(jobs_fts) VALUES ('optimize')")
        connection.executemany(
            "INSERT INTO meta VALUES (?, ?)", [*meta.items(), ("job_count", str(count))]
        )
        connection.commit()
        connection.execute("VACUUM")
        return count
    finally:
        connection.close()


def _flush(connection, batch_jobs: list, batch_fts: list) -> None:
    if not batch_jobs:
        return
    connection.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?)", batch_jobs)
    placeholders = ", ".join("?" * (len(FTS_COLUMNS) + 1))
    connection.executemany(
        f"INSERT INTO jobs_fts(rowid, {', '.join(FTS_COLUMNS)}) VALUES ({placeholders})",
        batch_fts,
    )
    batch_jobs.clear()
    batch_fts.clear()


def _write_pickle(documents, path: Path, meta: dict) -> int:
    jobs: list[dict] = []
    recent: list[float] = []
    title: dict[str, set[int]] = defaultdict(set)
    text: dict[str, set[int]] = defaultdict(set)
    for position, (job, columns, recency) in enumerate(documents):
        jobs.append(job)
        recent.append(recency)
        for token in columns["title"].split():
            title[token].add(position)
        for column in ("title", "skills", "search_blob"):
            for token in columns[column].split():
                text[token].add(position)
    payload = {
        "meta": {**meta, "job_count": str(len(jobs))},
        "jobs": jobs,
        "recent": recent,
        "postings": {"title": dict(title), "text": dict(text)},
    }
    with open(path, "wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    return len(jobs)


def resolve_format(fmt: str | None = None) -> str:
    fmt = (fmt or index_format_setting()).lower()
    if fmt == "auto":
        return FORMAT_SQLITE if fts5_available() else FORMAT_PICKLE
    if fmt == FORMAT_SQLITE and not fts5_available():
        raise RuntimeError("sqlite3 in this runtime has no FTS5; use the pickle format")
    return fmt


def build_from_items(items: Iterable[dict], path: Path | str, *, fmt: str | None = None,
                     now: datetime | None = None,
                     scan_started_at: datetime | None = None) -> BuildResult:
    """Write an uncompressed index file from job items (DynamoDB or eval corpus)."""
    fmt = resolve_format(fmt)
    now = now or datetime.now(timezone.utc)
    scan_started_at = scan_started_at or now
    path = Path(path)
    meta = {
        "built_at": now.isoformat(),
        "scan_started_at": scan_started_at.isoformat(),
        "format": fmt,
        "schema": SCHEMA_VERSION,
    }
    documents = _documents(items, now)
    if fmt == FORMAT_SQLITE:
        count = _write_sqlite(documents, path, meta)
    else:
        count = _write_pickle(documents, path, meta)
    return BuildResult(path, fmt, count, now, scan_started_at)


def _gzip(source: Path, target: Path) -> None:
    with open(source, "rb") as raw, gzip.open(target, "wb", compresslevel=6) as packed:
        shutil.copyfileobj(raw, packed, 1024 * 1024)


def _existing_built_at(s3, bucket: str) -> datetime | None:
    newest = None
    for fmt in (FORMAT_SQLITE, FORMAT_PICKLE):
        try:
            head = s3.head_object(Bucket=bucket, Key=index_key(fmt))
        except ClientError:
            continue
        built = parse_time(head.get("Metadata", {}).get("built_at"))
        if built and (newest is None or built > newest):
            newest = built
    return newest


def build_and_upload(*, bucket: str | None = None, table=None, s3_client=None,
                     force: bool = False, fmt: str | None = None,
                     work_dir: str | None = None) -> dict:
    """Scan DynamoDB, build the index and upload it gzipped to S3.

    Without ``force`` a build is skipped when the index in S3 is younger than
    ``VIECLAMBOT_SEARCH_INDEX_MIN_INTERVAL_SECONDS`` (duplicate schedule events).
    """
    from src.config import get_settings

    settings = get_settings()
    bucket = bucket or settings.s3_data_lake_bucket
    s3 = s3_client or boto3.client("s3", region_name=settings.aws_region)
    if table is None:
        table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
            settings.dynamodb_jobs_table
        )
    if not force:
        previous = _existing_built_at(s3, bucket)
        min_interval = env_int("VIECLAMBOT_SEARCH_INDEX_MIN_INTERVAL_SECONDS", 600)
        if previous and (datetime.now(timezone.utc) - previous).total_seconds() < min_interval:
            logger.info("Search index is fresh (built %s); skipping build", previous.isoformat())
            return {"status": "skipped", "built_at": previous.isoformat()}

    fmt = resolve_format(fmt)
    info = sqlite_info()
    started = time.monotonic()
    scan_started_at = datetime.now(timezone.utc)
    directory = Path(work_dir or os.environ.get("VIECLAMBOT_SEARCH_INDEX_DIR")
                     or tempfile.gettempdir())
    directory.mkdir(parents=True, exist_ok=True)
    raw_path = directory / f"vieclambot-index-build.{fmt}"
    gz_path = directory / f"vieclambot-index-build.{fmt}.gz"
    key = index_key(fmt)
    try:
        result = build_from_items(
            scan_items(table), raw_path, fmt=fmt, scan_started_at=scan_started_at,
        )
        build_seconds = time.monotonic() - started
        _gzip(raw_path, gz_path)
        raw_bytes, gz_bytes = raw_path.stat().st_size, gz_path.stat().st_size
        s3.upload_file(
            str(gz_path), bucket, key,
            ExtraArgs={"Metadata": result.metadata(), "ContentType": "application/gzip"},
        )
    finally:
        raw_path.unlink(missing_ok=True)
        gz_path.unlink(missing_ok=True)
    summary = {
        "status": "success",
        "format": fmt,
        "key": f"s3://{bucket}/{key}",
        "job_count": result.job_count,
        "built_at": result.built_at.isoformat(),
        "build_seconds": round(build_seconds, 2),
        "total_seconds": round(time.monotonic() - started, 2),
        "raw_bytes": raw_bytes,
        "gzip_bytes": gz_bytes,
        **info,
    }
    logger.info("Search index uploaded: %s", summary)
    return summary


__all__ = ["BuildResult", "build_and_upload", "build_from_items", "resolve_format", "scan_items"]
