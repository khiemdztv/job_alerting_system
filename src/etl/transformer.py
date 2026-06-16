"""
ETL Transformer for ViecLamBot.

Transforms RawJob objects into normalized Job objects:
1. Normalize title (lowercase, strip extra whitespace)
2. Parse salary (extract min/max VND from various formats)
3. Normalize location (standardize city names)
4. Extract job type
5. Extract tags/skills from description
6. Generate deterministic job ID
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from src.common.logger import get_logger
from src.common.models import Job, JobType, RawJob

logger = get_logger(__name__)


# ── Location normalization mapping ───────────────────────────────

LOCATION_MAP: dict[str, str] = {
    # Ho Chi Minh variants
    "hồ chí minh": "Ho Chi Minh",
    "ho chi minh": "Ho Chi Minh",
    "hcm": "Ho Chi Minh",
    "tp.hcm": "Ho Chi Minh",
    "tp hcm": "Ho Chi Minh",
    "tphcm": "Ho Chi Minh",
    "sài gòn": "Ho Chi Minh",
    "saigon": "Ho Chi Minh",
    "quận 1": "Ho Chi Minh",
    "quận 7": "Ho Chi Minh",
    "thủ đức": "Ho Chi Minh",
    # Ha Noi variants
    "hà nội": "Ha Noi",
    "ha noi": "Ha Noi",
    "hanoi": "Ha Noi",
    "hn": "Ha Noi",
    # Da Nang
    "đà nẵng": "Da Nang",
    "da nang": "Da Nang",
    "danang": "Da Nang",
    # Other cities
    "bình dương": "Binh Duong",
    "đồng nai": "Dong Nai",
    "hải phòng": "Hai Phong",
    "cần thơ": "Can Tho",
    "bắc ninh": "Bac Ninh",
    "hưng yên": "Hung Yen",
}

# ── Job type keywords ───────────────────────────────────────────

JOB_TYPE_KEYWORDS: dict[str, JobType] = {
    "full-time": JobType.FULL_TIME,
    "full time": JobType.FULL_TIME,
    "toàn thời gian": JobType.FULL_TIME,
    "part-time": JobType.PART_TIME,
    "part time": JobType.PART_TIME,
    "bán thời gian": JobType.PART_TIME,
    "contract": JobType.CONTRACT,
    "hợp đồng": JobType.CONTRACT,
    "internship": JobType.INTERNSHIP,
    "thực tập": JobType.INTERNSHIP,
    "remote": JobType.REMOTE,
    "từ xa": JobType.REMOTE,
    "work from home": JobType.REMOTE,
    "freelance": JobType.FREELANCE,
}

# ── Common tech skills for tag extraction ────────────────────────

TECH_SKILLS = [
    "python", "java", "javascript", "typescript", "sql", "nosql",
    "aws", "azure", "gcp", "docker", "kubernetes", "k8s",
    "spark", "hadoop", "kafka", "airflow", "dbt",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
    "react", "angular", "vue", "node.js", "nodejs",
    "terraform", "ansible", "jenkins", "ci/cd", "git",
    "machine learning", "deep learning", "ai", "nlp",
    "data engineer", "data analyst", "data scientist",
    "etl", "elt", "data pipeline", "data warehouse",
    "tableau", "power bi", "looker", "superset",
    "scala", "golang", "go", "rust", "c++", "c#", ".net",
    "linux", "agile", "scrum", "microservices", "api",
    "big data", "data lake", "snowflake", "redshift", "bigquery",
]


class Transformer:
    """Transform RawJob into normalized Job objects."""

    def transform(self, raw_job: RawJob) -> Optional[Job]:
        """Transform a single RawJob into a Job.

        Args:
            raw_job: Raw job data from scraper.

        Returns:
            Normalized Job, or None if transformation fails.
        """
        try:
            title_normalized = self._normalize_text(raw_job.title)

            job = Job(
                title=raw_job.title,
                title_normalized=title_normalized,
                company=raw_job.company,
                location=raw_job.location,
                location_normalized=self._normalize_location(raw_job.location),
                salary_raw=raw_job.salary_raw,
                description=raw_job.description,
                requirements=raw_job.requirements,
                job_type=self._parse_job_type(raw_job),
                tags=self._extract_tags(raw_job),
                source=raw_job.source,
                source_url=raw_job.source_url,
                posted_at=self._parse_date(raw_job.posted_at_raw, raw_job.scraped_at),
                scraped_at=raw_job.scraped_at,
                is_new=True,
            )

            # Parse salary
            salary_min, salary_max = self._parse_salary(raw_job.salary_raw)
            job.salary_min = salary_min
            job.salary_max = salary_max

            # Generate deterministic ID
            job.generate_id()

            return job

        except Exception as e:
            logger.warning(f"Transform failed for '{raw_job.title}': {e}")
            return None

    def transform_batch(self, raw_jobs: list[RawJob]) -> list[Job]:
        """Transform a batch of RawJobs.

        Args:
            raw_jobs: List of raw jobs.

        Returns:
            List of successfully transformed jobs.
        """
        jobs = []
        for raw_job in raw_jobs:
            job = self.transform(raw_job)
            if job:
                jobs.append(job)

        logger.info(
            f"Transformed {len(jobs)}/{len(raw_jobs)} jobs",
            extra={"job_count": len(jobs)},
        )
        return jobs

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize text: lowercase, strip, collapse whitespace."""
        text = text.lower().strip()
        text = re.sub(r"\s+", " ", text)
        # Normalize unicode (e.g., combine diacritics)
        text = unicodedata.normalize("NFC", text)
        return text

    @staticmethod
    def _normalize_location(location: str) -> str:
        """Normalize Vietnamese location to standard form."""
        if not location:
            return ""

        loc_lower = location.lower().strip()

        # Direct match
        for key, normalized in LOCATION_MAP.items():
            if key in loc_lower:
                return normalized

        # Return original if no mapping found
        return location.strip()

    @staticmethod
    def _parse_salary(salary_raw: str) -> tuple[Optional[int], Optional[int]]:
        """Parse salary string into min/max VND values.

        Handles formats:
        - "10 - 14 triệu"  → (10_000_000, 14_000_000)
        - "15 triệu"       → (15_000_000, 15_000_000)
        - "1000 - 2000 USD" → (25_000_000, 50_000_000)  # ~25k VND/USD
        - "Thỏa thuận"     → (None, None)
        - "$1000-2000"     → (25_000_000, 50_000_000)
        - "Lên đến 20 triệu" → (None, 20_000_000)
        """
        if not salary_raw:
            return None, None

        salary = salary_raw.lower().strip()

        # Skip negotiable
        if any(kw in salary for kw in ["thỏa thuận", "thương lượng", "negotiable", "cạnh tranh"]):
            return None, None

        # Detect currency
        multiplier = 1
        if "usd" in salary or "$" in salary:
            multiplier = 25_000  # Approximate VND/USD

        # Extract numbers
        numbers = re.findall(r"[\d.,]+", salary)
        numbers = [float(n.replace(",", "").replace(".", "")) for n in numbers if n]

        if not numbers:
            return None, None

        # Handle "triệu" (million VND)
        if "triệu" in salary or "tr" in salary:
            numbers = [n * 1_000_000 for n in numbers]
        elif multiplier > 1:
            numbers = [n * multiplier for n in numbers]

        salary_min = int(numbers[0]) if numbers else None
        salary_max = int(numbers[-1]) if len(numbers) > 1 else salary_min

        return salary_min, salary_max

    @staticmethod
    def _parse_job_type(raw_job: RawJob) -> JobType:
        """Determine job type from raw data."""
        search_text = f"{raw_job.job_type_raw} {raw_job.title} {raw_job.description}".lower()

        for keyword, job_type in JOB_TYPE_KEYWORDS.items():
            if keyword in search_text:
                return job_type

        return JobType.UNKNOWN

    @staticmethod
    def _extract_tags(raw_job: RawJob) -> list[str]:
        """Extract technology tags/skills from job data."""
        search_text = f"{raw_job.title} {raw_job.description} {raw_job.requirements}".lower()
        found_tags = []

        for skill in TECH_SKILLS:
            # Word boundary matching for short skills
            if len(skill) <= 3:
                pattern = rf"\b{re.escape(skill)}\b"
                if re.search(pattern, search_text):
                    found_tags.append(skill)
            elif skill in search_text:
                found_tags.append(skill)

        return sorted(set(found_tags))

    @staticmethod
    def _parse_date(date_str: str, base_date: Optional[datetime] = None) -> Optional[datetime]:
        """Parse various date formats to datetime, including relative times."""
        if not date_str:
            return None

        if base_date is None:
            base_date = datetime.now(timezone.utc)

        date_str = date_str.lower().strip()

        # Try ISO format
        try:
            return datetime.fromisoformat(date_str.replace("z", "+00:00").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

        # Try common absolute formats
        for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"]:
            try:
                return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

        # Try parsing relative times (e.g., "2 hours ago", "3 days ago", "1 ngày trước", etc.)
        import re
        from datetime import timedelta

        # Extract number
        match = re.search(r"(\d+)", date_str)
        if match:
            val = int(match.group(1))
            # English units
            if "hour" in date_str or "hr" in date_str:
                return base_date - timedelta(hours=val)
            elif "day" in date_str:
                return base_date - timedelta(days=val)
            elif "week" in date_str:
                return base_date - timedelta(weeks=val)
            elif "month" in date_str:
                return base_date - timedelta(days=val * 30)
            # Vietnamese units
            elif "giờ" in date_str or "g" in date_str:
                return base_date - timedelta(hours=val)
            elif "ngày" in date_str:
                return base_date - timedelta(days=val)
            elif "tuần" in date_str:
                return base_date - timedelta(weeks=val)
            elif "tháng" in date_str:
                return base_date - timedelta(days=val * 30)

        if "vừa xong" in date_str or "mới đây" in date_str or "just now" in date_str:
            return base_date

        return None
