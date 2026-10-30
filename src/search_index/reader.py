"""Load the prebuilt search index from S3 and return candidates for rank_jobs.

``SearchIndex.load(bucket)`` keeps one index per warm container.  It re-checks S3
(``HEAD`` of both index objects) at most every ``VIECLAMBOT_SEARCH_INDEX_CHECK_SECONDS``
(60 s) and downloads to ``/tmp`` only when the chosen object's ETag changed.  The
newest usable object wins: the SQLite file needs FTS5 in *this* runtime, the
pickle fallback works everywhere.

Candidate retrieval (:meth:`SearchIndex.candidates`) is a superset of what
:func:`src.matcher.search.relevance` can score above zero: relevance() requires
every concept group of one comma-separated alternative to appear in the title,
tags or search blob, so the index returns the union over alternatives of
(AND over groups of (OR over accent-folded synonym phrases)).  Accent
signatures, title-only abbreviations and the company-name exclusion are left to
rank_jobs.  With ``include_partial=True`` jobs matching *any* group are appended
after those (diagnostics; they can never score).
"""

from __future__ import annotations

import gzip
import io
import json
import os
import pickle
import shutil
import sqlite3
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.common.logger import get_logger
from src.search_index.common import (
    FORMAT_PICKLE,
    FORMAT_SQLITE,
    TEXT_COLUMNS,
    Alternative,
    Term,
    env_int,
    fts5_available,
    index_key,
    parse_time,
    retrieval_plan,
)

logger = get_logger(__name__)

DEFAULT_CANDIDATE_LIMIT = 2000
_ROW_CHUNK = 500

# Tiers used to order candidates before a limit cuts them; they mirror the score
# tiers of relevance() so a truncated set keeps the jobs rank_jobs would put first.
_TIER_PHRASE_IN_TITLE = 4  # can score 100
_TIER_ALL_IN_TITLE = 3  # can score 80
_TIER_ALL_ANYWHERE = 2  # can score 30-60
_TIER_PARTIAL = 1  # cannot score; only with include_partial


class _RestrictedUnpickler(pickle.Unpickler):
    """The pickle index only holds builtins; refuse every global (no code execution)."""

    def find_class(self, module, name):  # pragma: no cover - only hit by tampered files
        raise pickle.UnpicklingError(f"global '{module}.{name}' is not allowed")


# ── Backends ────────────────────────────────────────────────────────────────────


class _SqliteBackend:
    def __init__(self, path: Path):
        self.path = path
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.execute("PRAGMA query_only = 1")
        self.meta = dict(self.connection.execute("SELECT key, value FROM meta"))
        self.recent = dict(self.connection.execute("SELECT id, recent FROM jobs"))

    def match(self, title_only: bool, terms: tuple[Term, ...]) -> set[int]:
        columns = "title" if title_only else " ".join(TEXT_COLUMNS)
        expression = " OR ".join('"' + " ".join(term) + '"' for term in terms)
        query = "{" + columns + "} : (" + expression + ")"
        return {row[0] for row in self.connection.execute(
            "SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?", (query,)
        )}

    def rows(self, ids: list[int]) -> list[dict]:
        placeholders = ", ".join("?" * len(ids))
        data = dict(self.connection.execute(
            f"SELECT id, data FROM jobs WHERE id IN ({placeholders})", ids
        ))
        return [json.loads(zlib.decompress(data[i])) for i in ids if i in data]

    def close(self) -> None:
        self.connection.close()


class _PickleBackend:
    def __init__(self, path: Path):
        self.path = path
        with open(path, "rb") as handle:
            payload = _RestrictedUnpickler(handle).load()
        self.meta = payload["meta"]
        self.jobs: list[dict] = payload["jobs"]
        self.recent = dict(enumerate(payload["recent"]))
        self.postings: dict[str, dict[str, set[int]]] = payload["postings"]

    def match(self, title_only: bool, terms: tuple[Term, ...]) -> set[int]:
        postings = self.postings["title" if title_only else "text"]
        found: set[int] = set()
        for term in terms:
            # Every token of the phrase must occur (adjacency is left to rank_jobs).
            sets = sorted((postings.get(token, set()) for token in term), key=len)
            if sets and sets[0]:
                found |= set.intersection(*sets)
        return found

    def rows(self, ids: list[int]) -> list[dict]:
        return [self.jobs[i] for i in ids]

    def close(self) -> None:
        pass


# ── Index ───────────────────────────────────────────────────────────────────────


@dataclass
class SearchIndex:
    backend: object
    format: str
    etag: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def built_at(self) -> datetime | None:
        return parse_time(self.backend.meta.get("built_at"))

    @property
    def scan_started_at(self) -> datetime | None:
        return parse_time(self.backend.meta.get("scan_started_at")) or self.built_at

    @property
    def job_count(self) -> int:
        return int(self.backend.meta.get("job_count") or len(self.backend.recent))

    def age_seconds(self, now: datetime | None = None) -> float:
        built = self.built_at
        if built is None:
            return float("inf")
        return ((now or datetime.now(timezone.utc)) - built).total_seconds()

    # -- loading ---------------------------------------------------------------

    @classmethod
    def open_file(cls, path: Path | str, fmt: str | None = None, etag: str = "") -> SearchIndex:
        """Open an uncompressed local index file (eval, tests, after download)."""
        path = Path(path)
        if fmt is None:
            with open(path, "rb") as handle:
                fmt = FORMAT_SQLITE if handle.read(16).startswith(b"SQLite format 3") \
                    else FORMAT_PICKLE
        backend = _SqliteBackend(path) if fmt == FORMAT_SQLITE else _PickleBackend(path)
        return cls(backend=backend, format=fmt, etag=etag)

    @classmethod
    def load(cls, bucket: str, *, s3_client=None) -> SearchIndex | None:
        """The current index for ``bucket``, cached per container; None when absent."""
        with _CACHE_LOCK:
            entry = _CACHE.get(bucket)
            now = time.monotonic()
            check_seconds = env_int("VIECLAMBOT_SEARCH_INDEX_CHECK_SECONDS", 60)
            if entry is not None and now - entry.checked_at < check_seconds:
                return entry.index
            try:
                index = _refresh(bucket, entry, s3_client)
            except (ClientError, BotoCoreError, OSError, sqlite3.Error,
                     pickle.UnpicklingError, EOFError, KeyError, ValueError):
                logger.warning("Search index refresh failed for %s", bucket, exc_info=True)
                # Keep serving the copy we have; the caller still checks its age.
                index = entry.index if entry else None
            _CACHE[bucket] = _CacheEntry(index=index, checked_at=now)
            return index

    def close(self) -> None:
        self.backend.close()

    # -- retrieval -------------------------------------------------------------

    def _tiers(self, queries: Iterable[str], include_partial: bool) -> dict[int, int]:
        tiers: dict[int, int] = {}

        def promote(ids: Iterable[int], tier: int) -> None:
            for i in ids:
                if tiers.get(i, 0) < tier:
                    tiers[i] = tier

        for query in queries:
            for alternative in retrieval_plan(str(query or "")):
                self._alternative_tiers(alternative, include_partial, promote)
        return tiers

    def _alternative_tiers(self, alternative: Alternative, include_partial: bool,
                           promote) -> None:
        anywhere = [self.backend.match(False, group) for group in alternative.groups]
        everywhere = set.intersection(*anywhere) if anywhere else set()
        if include_partial:
            promote(set().union(*anywhere) - everywhere, _TIER_PARTIAL)
        if not everywhere:
            return
        promote(everywhere, _TIER_ALL_ANYWHERE)
        in_title = everywhere
        for group in alternative.groups:
            in_title = in_title & self.backend.match(True, group)
            if not in_title:
                return
        promote(in_title, _TIER_ALL_IN_TITLE)
        if alternative.phrase:
            promote(in_title & self.backend.match(True, (alternative.phrase,)),
                    _TIER_PHRASE_IN_TITLE)

    def candidates(self, query_variants, location: str | None = None,
                   limit: int | None = DEFAULT_CANDIDATE_LIMIT, *,
                   salary_min: int | None = None, salary_max: int | None = None,
                   since: datetime | None = None, posted_since: datetime | None = None,
                   max_age_days: int = 60, include_partial: bool = False) -> list[dict]:
        """Job dicts (``SEARCH_FIELDS`` and a few extras) for ``rank_jobs``.

        Candidates pass ``matches_filters`` (location, salary, dates, expiry) and
        are ordered by the best score tier they can reach, newest first, before
        ``limit`` cuts them; ``limit=None`` returns every candidate.
        """
        from src.matcher.search import matches_filters

        if isinstance(query_variants, str):
            query_variants = [query_variants]
        now = datetime.now(timezone.utc)
        with self.lock:
            tiers = self._tiers(query_variants, include_partial)
            recent = self.backend.recent
            order = sorted(tiers, key=lambda i: (tiers[i], recent.get(i, 0.0), -i),
                           reverse=True)
            result: list[dict] = []
            for start in range(0, len(order), _ROW_CHUNK):
                for job in self.backend.rows(order[start:start + _ROW_CHUNK]):
                    if not matches_filters(
                        job, location=location, salary_min=salary_min, since=since,
                        max_age_days=max_age_days, now=now, posted_since=posted_since,
                        salary_max=salary_max,
                    ):
                        continue
                    result.append(job)
                    if limit is not None and len(result) >= limit:
                        return result
            return result


# ── Container cache ─────────────────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    index: SearchIndex | None
    checked_at: float


_CACHE: dict[str, _CacheEntry] = {}
_CACHE_LOCK = threading.RLock()
_NOT_FOUND = {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


def reset_cache() -> None:
    """Forget every cached index (tests; a new deployment does this implicitly)."""
    with _CACHE_LOCK:
        for entry in _CACHE.values():
            if entry.index is not None:
                try:
                    entry.index.close()
                except Exception:  # pragma: no cover - best effort
                    pass
        _CACHE.clear()


def _cache_dir() -> Path:
    directory = Path(os.environ.get("VIECLAMBOT_SEARCH_INDEX_DIR") or tempfile.gettempdir())
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _client(s3_client):
    if s3_client is not None:
        return s3_client
    from src.config import get_settings

    return boto3.client("s3", region_name=get_settings().aws_region)


def _heads(s3, bucket: str) -> list[tuple[datetime, str, str]]:
    """(built_at, format, etag) of every index object this runtime can read, newest first."""
    found = []
    for fmt in (FORMAT_SQLITE, FORMAT_PICKLE):
        if fmt == FORMAT_SQLITE and not fts5_available():
            continue
        try:
            head = s3.head_object(Bucket=bucket, Key=index_key(fmt))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in _NOT_FOUND:
                continue
            raise
        built = parse_time(head.get("Metadata", {}).get("built_at")) \
            or head.get("LastModified")
        found.append((built or datetime.min.replace(tzinfo=timezone.utc), fmt,
                      head.get("ETag", "")))
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def _refresh(bucket: str, entry: _CacheEntry | None, s3_client) -> SearchIndex | None:
    s3 = _client(s3_client)
    heads = _heads(s3, bucket)
    if not heads:
        if entry is None or entry.index is not None:
            logger.info("No search index in s3://%s/%s", bucket, index_key(FORMAT_SQLITE))
        return None
    _, fmt, etag = heads[0]
    current = entry.index if entry else None
    if current is not None and current.etag == etag and current.format == fmt:
        return current
    started = time.monotonic()
    response = s3.get_object(Bucket=bucket, Key=index_key(fmt))
    etag = response.get("ETag", etag)
    safe = "".join(ch for ch in etag if ch.isalnum())[:40] or "current"
    target = _cache_dir() / f"vieclambot-index-{safe}.{fmt}"
    partial = target.with_suffix(target.suffix + ".part")
    with gzip.GzipFile(fileobj=_Body(response["Body"])) as packed, open(partial, "wb") as out:
        shutil.copyfileobj(packed, out, 1024 * 1024)
    os.replace(partial, target)
    index = SearchIndex.open_file(target, fmt, etag=etag)
    logger.info(
        "Search index loaded: format=%s jobs=%s built_at=%s etag=%s in %.0f ms",
        fmt, index.job_count, index.backend.meta.get("built_at"), etag,
        (time.monotonic() - started) * 1000,
    )
    if current is not None:
        old_path = current.backend.path
        current.close()
        if old_path != target:
            try:
                old_path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - Windows keeps open files locked
                pass
    return index


class _Body(io.RawIOBase):
    """Adapt a botocore StreamingBody to the file API GzipFile expects."""

    def __init__(self, body):
        self.body = body

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self.body.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


__all__ = ["DEFAULT_CANDIDATE_LIMIT", "SearchIndex", "reset_cache"]
