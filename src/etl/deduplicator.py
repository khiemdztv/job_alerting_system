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
from typing import Optional

from src.common.logger import get_logger
from src.common.models import Job

logger = get_logger(__name__)


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
        raw = f"{job.title_normalized}|{job.company.lower().strip()}|{job.source.value}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def compute_cross_source_hash(self, job: Job) -> str:
        """Compute hash ignoring source (for cross-source dedup).

        Uses only title + company, so the same job across different
        sources will be detected as duplicate.

        Args:
            job: Normalized job object.

        Returns:
            16-char hex hash string.
        """
        raw = f"{job.title_normalized}|{job.company.lower().strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

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

    def deduplicate(self, jobs: list[Job], cross_source: bool = True) -> list[Job]:
        """Remove duplicates from a list of jobs.

        Args:
            jobs: List of jobs to deduplicate.
            cross_source: If True, dedup across sources (same job on
                         CareerLink and Jooble = 1 result).

        Returns:
            Deduplicated list of jobs.
        """
        self.reset()
        unique_jobs: list[Job] = []

        for job in jobs:
            if not self.is_duplicate(job, cross_source=cross_source):
                unique_jobs.append(job)

        dedup_count = len(jobs) - len(unique_jobs)
        if dedup_count > 0:
            logger.info(
                f"Deduplication: {len(jobs)} → {len(unique_jobs)} "
                f"({dedup_count} duplicates removed)",
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
