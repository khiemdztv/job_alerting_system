"""
Data Quality Validators for ViecLamBot.

Validates job data at multiple stages:
1. Raw validation (post-scrape)
2. Processed validation (post-transform)
3. Quality metrics reporting
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.common.logger import get_logger
from src.common.models import Job, RawJob
from src.config import get_settings

logger = get_logger(__name__)


@dataclass
class QualityReport:
    """Quality check results for a batch of jobs."""

    total_input: int = 0
    passed: int = 0
    failed: int = 0
    warnings: int = 0

    # Specific checks
    missing_title: int = 0
    title_too_short: int = 0
    missing_company: int = 0
    missing_location: int = 0
    missing_salary: int = 0
    missing_url: int = 0
    duplicate_titles: int = 0

    # Timestamps
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total_input * 100) if self.total_input > 0 else 0.0

    @property
    def completeness_score(self) -> float:
        """Score based on field completeness (0-100)."""
        if self.total_input == 0:
            return 0.0

        total_fields = self.total_input * 5  # title, company, location, salary, url
        missing = (
            self.missing_title
            + self.missing_company
            + self.missing_location
            + self.missing_salary
            + self.missing_url
        )
        return ((total_fields - missing) / total_fields * 100) if total_fields > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "total_input": self.total_input,
            "passed": self.passed,
            "failed": self.failed,
            "warnings": self.warnings,
            "pass_rate": round(self.pass_rate, 1),
            "completeness_score": round(self.completeness_score, 1),
            "missing_title": self.missing_title,
            "title_too_short": self.title_too_short,
            "missing_company": self.missing_company,
            "missing_location": self.missing_location,
            "missing_salary": self.missing_salary,
            "missing_url": self.missing_url,
            "duplicate_titles": self.duplicate_titles,
            "checked_at": self.checked_at,
        }


class RawJobValidator:
    """Validate raw jobs immediately after scraping."""

    def __init__(self):
        self.settings = get_settings()

    def validate(self, raw_job: RawJob) -> tuple[bool, list[str]]:
        """Validate a single raw job.

        Args:
            raw_job: Raw job to validate.

        Returns:
            (is_valid, list_of_issues)
        """
        issues: list[str] = []

        # Critical: must have title
        if not raw_job.title or not raw_job.title.strip():
            issues.append("CRITICAL: Missing title")
            return False, issues

        # Title length check
        if len(raw_job.title.strip()) < self.settings.min_title_length:
            issues.append(f"WARNING: Title too short ({len(raw_job.title)} chars)")

        # Warnings (non-blocking)
        if not raw_job.company:
            issues.append("WARNING: Missing company")
        if not raw_job.source_url:
            issues.append("WARNING: Missing source URL")

        is_valid = not any(i.startswith("CRITICAL") for i in issues)
        return is_valid, issues

    def validate_batch(self, raw_jobs: list[RawJob]) -> tuple[list[RawJob], QualityReport]:
        """Validate a batch of raw jobs.

        Args:
            raw_jobs: List of raw jobs to validate.

        Returns:
            (valid_jobs, quality_report)
        """
        report = QualityReport(total_input=len(raw_jobs))
        valid_jobs: list[RawJob] = []
        seen_titles: set[str] = set()

        for job in raw_jobs:
            is_valid, issues = self.validate(job)

            # Track specific issues
            for issue in issues:
                if "Missing title" in issue:
                    report.missing_title += 1
                elif "Title too short" in issue:
                    report.title_too_short += 1
                elif "Missing company" in issue:
                    report.missing_company += 1
                elif "Missing source URL" in issue:
                    report.missing_url += 1

            if not job.location:
                report.missing_location += 1
            if not job.salary_raw:
                report.missing_salary += 1

            # Check for duplicate titles
            title_key = job.title.lower().strip()
            if title_key in seen_titles:
                report.duplicate_titles += 1
            else:
                seen_titles.add(title_key)

            if is_valid:
                report.passed += 1
                valid_jobs.append(job)
            else:
                report.failed += 1
                report.warnings += len([i for i in issues if i.startswith("WARNING")])

        logger.info(
            f"Quality check: {report.passed}/{report.total_input} passed "
            f"({report.pass_rate:.1f}%), "
            f"completeness: {report.completeness_score:.1f}%",
        )

        return valid_jobs, report


class ProcessedJobValidator:
    """Validate processed jobs before loading to DB."""

    def validate(self, job: Job) -> tuple[bool, list[str]]:
        """Validate a processed job.

        Args:
            job: Processed job to validate.

        Returns:
            (is_valid, list_of_issues)
        """
        issues: list[str] = []

        if not job.title_normalized:
            issues.append("CRITICAL: Missing normalized title")
        if not job.job_id and not job.computed_job_id:
            issues.append("CRITICAL: Missing job ID")
        if not job.source_url:
            issues.append("WARNING: Missing source URL")

        # Salary sanity check
        if job.salary_min and job.salary_max:
            if job.salary_min > job.salary_max:
                issues.append("WARNING: salary_min > salary_max")
            if job.salary_min < 0:
                issues.append("WARNING: Negative salary")

        is_valid = not any(i.startswith("CRITICAL") for i in issues)
        return is_valid, issues
