"""
Deduplicator for ViecLamBot.

Uses content-hash-based deduplication to prevent storing the same job
from multiple sources or repeated scraping runs.

Strategy:
1. Generate SHA256 hash from (normalized_title + company + source)
2. Check if hash exists in DynamoDB before inserting
3. For jobs from different sources with same title+company, keep both
   but link them (different source = different job_id)
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any, TypeVar, Union

from src.common.logger import get_logger
from src.common.models import Job
from src.common.text_utils import clean_vn_text, fold_vn_text, normalize_vn_text

logger = get_logger(__name__)

T = TypeVar("T", bound=Union[Job, Mapping])

# ── Cross-board grouping (canonical_group_id) ────────────────────

# Legal forms and filler words that boards add to or drop from a company name.
_COMPANY_NOISE = re.compile(
    r"(?<![a-z0-9])(?:cong ty|cty|ct|tnhh|trach nhiem huu han|mot thanh vien|hai thanh vien"
    r"|mtv|co phan|cp|cpdt|jsc|joint stock company|joint stock|company limited|company|co"
    r"|ltd|limited|corp|corporation|inc|llc|plc|pte|tap doan|chi nhanh|branch|van phong dai dien"
    r"|vpdd|viet nam|vietnam|vn|thuong mai|dich vu|tm|dv|tmdv|sx|xnk|san xuat|xuat nhap khau"
    r"|dau tu|group)(?![a-z0-9])"
)
# Seniority and recruiting noise; the posting is the same job at another level of
# hype ("Senior Java Developer (Urgent)" on one board, "Java Developer" on another).
_TITLE_NOISE = re.compile(
    r"(?<![a-z0-9])(?:senior|junior|middle|mid level|mid|fresher|sr|jr|level|lv|i{1,3}"
    r"|nhan vien|chuyen vien|nv|cv|tuyen gap|can tuyen gap|can tuyen|tuyen dung|dang tuyen"
    r"|tuyen|gap|urgent|hot|hiring|we are hiring|cao cap|full time|fulltime|part time"
    r"|parttime|\d+ (?:nguoi|nv|ung vien|vi tri|slots?))(?![a-z0-9])"
)
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}|【[^】]*】")
# Tails boards append to titles: "- Lương 15-20 triệu", "Tại Hà Nội", "Upto 2000$".
_TITLE_TAIL = re.compile(
    r"(?:\s[-–|,:]\s*|\s)(?:lương|thu nhập|salary|upto|up to|lên đến|tại|at|làm việc tại"
    r"|khu vực|địa điểm)\s.*$|\s[-–|]\s*(?:hà nội|hồ chí minh|tp\.? ?hcm|hcm|đà nẵng)(?![\w]).*$"
)


def normalize_company(company: str) -> str:
    """"Công ty TNHH ABC Việt Nam" / "ABC Co., Ltd" / "ABC" → "abc"."""
    text = re.sub(r"[^a-z0-9]+", " ", clean_vn_text(company))
    text = _COMPANY_NOISE.sub(" ", text)
    return " ".join(text.split())


def normalize_title_for_group(title: str) -> str:
    """Folded title without brackets, salary/location tails and seniority noise."""
    text = _BRACKETS.sub(" ", normalize_vn_text(title))
    text = _TITLE_TAIL.sub(" ", text)
    text = re.sub(r"[^a-z0-9+#]+", " ", fold_vn_text(text))
    text = _TITLE_NOISE.sub(" ", text)
    words = text.split()
    if not words:  # "Senior (Urgent)" — keep something stable
        words = re.sub(r"[^a-z0-9+#]+", " ", clean_vn_text(title)).split()
    return " ".join(words)


def _field(job: Union[Job, Mapping], name: str) -> Any:
    return job.get(name) if isinstance(job, Mapping) else getattr(job, name, None)


def canonical_group_id(job: Union[Job, Mapping]) -> str:
    """Same posting on different boards → same 16-hex id.

    Key = normalised title + normalised company + city.  ``job_id`` is untouched.
    A posting without a company name is never grouped (its own source URL is
    mixed in), so two anonymous "Nhân viên kinh doanh" ads stay apart.
    """
    city = _field(job, "location_normalized") or ""
    if not city and _field(job, "location"):
        from src.etl.transformer import Transformer

        city = Transformer._normalize_location(str(_field(job, "location")))
    company = normalize_company(str(_field(job, "company") or ""))
    title = normalize_title_for_group(str(_field(job, "title") or ""))
    if company:
        raw = f"{title}|{company}|{clean_vn_text(str(city))}"
    else:
        identity = _field(job, "job_id") or _field(job, "source_url") or ""
        raw = f"{title}||{clean_vn_text(str(city))}|{identity}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def group_duplicates(jobs: Iterable[T]) -> dict[str, list[T]]:
    """``{canonical_group_id: [jobs...]}`` in first-seen order (Job objects or dicts).

    Uses the stored ``canonical_group_id`` when present, so the bot can merge
    DynamoDB items into one card ("Có trên TopCV, VietnamWorks").
    """
    groups: dict[str, list[T]] = {}
    for job in jobs:
        group_id = _field(job, "canonical_group_id") or canonical_group_id(job)
        groups.setdefault(str(group_id), []).append(job)
    return groups


class Deduplicator:
    """Hash-based job deduplication."""

    def __init__(self):
        self._seen_hashes: set[str] = set()

    def reset(self) -> None:
        """Clear the seen hashes (for new scraping runs)."""
        self._seen_hashes.clear()

    def compute_hash(self, job: Job) -> str:
        """Compute dedup hash for a job.

        Uses title + company + source to generate a unique fingerprint.
        This means the same job from different sources will be kept
        (useful for price comparison across platforms).

        Args:
            job: Normalized job object.

        Returns:
            16-char hex hash string.
        """
        return job.computed_job_id

    def compute_cross_source_hash(self, job: Job) -> str:
        """Compute hash ignoring source (for cross-source dedup).

        Uses only title + company, so the same job across different
        sources will be detected as duplicate.

        Args:
            job: Normalized job object.

        Returns:
            16-char hex hash string.
        """
        from src.matcher.search import identity
        return identity(job.to_dynamo_item())

    def is_duplicate(self, job: Job, cross_source: bool = False) -> bool:
        """Check if a job is a duplicate within the current batch.

        Args:
            job: Job to check.
            cross_source: If True, check across sources (ignore source in hash).

        Returns:
            True if duplicate, False if new.
        """
        if cross_source:
            hash_val = self.compute_cross_source_hash(job)
        else:
            hash_val = self.compute_hash(job)

        if hash_val in self._seen_hashes:
            return True

        self._seen_hashes.add(hash_val)
        return False

    @staticmethod
    def _completeness_score(job: Job) -> int:
        """Score a job by data completeness. Higher = more complete.

        Aggregator sources (jooble, careerjet) get a penalty since
        they re-crawl from primary sources and often have less data.
        """
        score = sum(bool(x) for x in [
            job.company, job.salary_raw, job.location,
            job.description, job.posted_at,
        ])
        # Aggregator penalty: prefer primary sources
        if job.source.value in ("jooble", "careerjet"):
            score -= 1
        return score

    def deduplicate(self, jobs: list[Job], cross_source: bool = True) -> list[Job]:
        """Remove duplicates, keeping the MOST COMPLETE record for each group.

        Unlike the old version that kept the first record encountered,
        this version compares completeness scores and keeps the best one.
        This prevents aggregators from shadowing primary sources AND ensures
        the most data-rich version is stored.

        Args:
            jobs: List of jobs to deduplicate.
            cross_source: If True, dedup across sources (same job on
                         CareerLink and Jooble = 1 result).

        Returns:
            Deduplicated list of jobs (best version of each).
        """
        best: dict[str, Job] = {}

        for job in jobs:
            h = self.compute_cross_source_hash(job) if cross_source else self.compute_hash(job)
            if h not in best or self._completeness_score(job) > self._completeness_score(best[h]):
                best[h] = job

        unique_jobs = list(best.values())
        dedup_count = len(jobs) - len(unique_jobs)
        if dedup_count > 0:
            logger.info(
                f"Deduplication: {len(jobs)} → {len(unique_jobs)} "
                f"({dedup_count} duplicates removed, kept best records)",
                extra={"job_count": len(unique_jobs)},
            )

        return unique_jobs

    def deduplicate_against_existing(
        self, new_jobs: list[Job], existing_ids: set[str]
    ) -> list[Job]:
        """Filter out jobs that already exist in the database.

        Args:
            new_jobs: Newly scraped jobs.
            existing_ids: Set of job_ids already in DynamoDB.

        Returns:
            Only truly new jobs.
        """
        truly_new = [j for j in new_jobs if j.computed_job_id not in existing_ids]

        skipped = len(new_jobs) - len(truly_new)
        if skipped > 0:
            logger.info(
                f"DB dedup: {len(new_jobs)} → {len(truly_new)} "
                f"({skipped} already exist in DB)",
            )

        return truly_new
