"""Settings, keys, tokenization and query planning shared by builder and reader."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import lru_cache

from src.common.text_utils import clean_vn_text

SCHEMA_VERSION = "1"
FORMAT_SQLITE = "sqlite"
FORMAT_PICKLE = "pickle"

# Fields the index stores besides ``SEARCH_FIELDS`` (when an item has them).
EXTRA_FIELDS = ("skills", "level", "job_type", "expires_at", "first_seen_at")

# FTS5 columns.  ``company`` is indexed but never used for candidate retrieval:
# relevance() ignores the employer name, so matching it would only add noise.
FTS_COLUMNS = ("title", "skills", "company", "search_blob")
TEXT_COLUMNS = ("title", "skills", "search_blob")
FTS_TOKENIZER = "unicode61 remove_diacritics 2"

# Same notion of a token as FTS5 unicode61: letters and digits, everything else
# separates.  Text is folded with clean_vn_text first because unicode61 does not
# fold "đ" to "d".
_TOKEN = re.compile(r"[^\W_]+")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def index_prefix() -> str:
    prefix = os.environ.get("VIECLAMBOT_SEARCH_INDEX_PREFIX", "index/")
    return prefix if not prefix or prefix.endswith("/") else prefix + "/"


def index_key(fmt: str) -> str:
    return index_prefix() + ("jobs.sqlite.gz" if fmt == FORMAT_SQLITE else "jobs.pickle.gz")


def index_format_setting() -> str:
    """``auto`` (SQLite when FTS5 works, else pickle), ``sqlite`` or ``pickle``."""
    value = os.environ.get("VIECLAMBOT_SEARCH_INDEX_FORMAT", "auto").strip().lower()
    return value if value in {"auto", FORMAT_SQLITE, FORMAT_PICKLE} else "auto"


@lru_cache(maxsize=1)
def fts5_available() -> bool:
    """True when this interpreter's sqlite3 can create an FTS5 unicode61 table."""
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                f"CREATE VIRTUAL TABLE probe USING fts5(a, tokenize='{FTS_TOKENIZER}')"
            )
            return True
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def sqlite_info() -> dict:
    """Diagnostics for logs: library version and FTS compile options."""
    info = {"sqlite_version": sqlite3.sqlite_version, "fts5": fts5_available()}
    try:
        connection = sqlite3.connect(":memory:")
        info["fts_options"] = [
            row[0] for row in connection.execute("PRAGMA compile_options") if "FTS" in row[0]
        ]
        connection.close()
    except sqlite3.Error:
        pass
    return info


def tokens(text) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(clean_vn_text(str(text or ""))))


def fold_text(*parts) -> str:
    """Accent-folded, space-joined text of every non-empty part (lists flattened)."""
    pieces = []
    for part in parts:
        if not part:
            continue
        if isinstance(part, (list, tuple, set, frozenset)):
            pieces.extend(str(p) for p in part if p)
        else:
            pieces.append(str(part))
    return " ".join(tokens(" ".join(pieces)))


def json_safe(value):
    """DynamoDB values (Decimal, sets) as plain JSON/pickle-safe Python values."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(json_safe(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def parse_time(value) -> datetime | None:
    from src.matcher.search import timestamp

    return timestamp(value) if value else None


# ── Retrieval plan ──────────────────────────────────────────────────────────────

Term = tuple[str, ...]  # tokens of one synonym, matched as an adjacent phrase


@dataclass(frozen=True)
class Alternative:
    """One comma-separated alternative of a query.

    ``groups`` are the concept groups relevance() requires *all* of; each group
    is an OR of synonym phrases.  ``phrase`` is the whole alternative, used to
    rank "the query phrase is in the title" candidates first.
    """

    phrase: Term
    groups: tuple[tuple[Term, ...], ...]


@lru_cache(maxsize=512)
def retrieval_plan(query: str) -> tuple[Alternative, ...]:
    """Candidate-retrieval view of ``src.matcher.search._query_plan``.

    The plan already applies filler removal, salary stripping and synonym
    expansion (``src.matcher.synonyms.expand_token``), so the index finds
    exactly the words relevance() will look for, accent-folded.  Accent
    signatures, title-only abbreviations and the company-name exclusion are
    *not* applied here: the candidate set is a superset and rank_jobs decides.
    """
    from src.matcher.search import _query_plan

    plan = []
    for phrase, groups in _query_plan(query):
        retrieval_groups = []
        for group in groups:
            terms = tuple(dict.fromkeys(tokens(term.folded) for term in group))
            if any(not term for term in terms):
                # A synonym without any letter/digit cannot be looked up; the
                # group cannot narrow the candidates, so it is left to rank_jobs.
                continue
            retrieval_groups.append(terms)
        if retrieval_groups:
            plan.append(Alternative(tokens(phrase), tuple(retrieval_groups)))
    return tuple(plan)
