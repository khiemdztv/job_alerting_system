"""
Pydantic data models for ViecLamBot.

Defines the core domain objects: Job, User, Subscription.
These models are used across all layers (scraping, ETL, storage, notification).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, computed_field, field_validator


# ── Enums ────────────────────────────────────────────────────────


class JobSource(str, Enum):
    """Supported job data sources."""
    JOOBLE = "jooble"
    CAREERLINK = "careerlink"
    VIECLAM24H = "vieclam24h"
    ITVIEC = "itviec"
    CAREERVIET = "careerviet"
    TIMVIEC365 = "timviec365"
    YBOX = "ybox"
    CHOTOT = "chotot"


class JobType(str, Enum):
    """Job employment type."""
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    REMOTE = "remote"
    FREELANCE = "freelance"
    UNKNOWN = "unknown"


# ── Job Model ────────────────────────────────────────────────────


class RawJob(BaseModel):
    """Raw job data as scraped from a source (before ETL)."""

    title: str
    company: str = ""
    location: str = ""
    salary_raw: str = ""
    description: str = ""
    requirements: str = ""
    job_type_raw: str = ""
    source: JobSource
    source_url: str = ""
    posted_at_raw: str = ""
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("title", "company", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        if isinstance(v, str):
            return v.strip()
        return v


class Job(BaseModel):
    """Processed and normalized job posting."""

    # Identity
    job_id: str = ""  # SHA256 hash, computed after creation
    title: str
    title_normalized: str = ""  # Lowercased, stripped for matching

    # Company & Location
    company: str = ""
    location: str = ""
    location_normalized: str = ""  # e.g., "ho chi minh", "ha noi"

    # Salary
    salary_raw: str = ""
    salary_min: Optional[int] = None  # VND
    salary_max: Optional[int] = None  # VND
    salary_currency: str = "VND"

    # Details
    description: str = ""
    requirements: str = ""
    job_type: JobType = JobType.UNKNOWN
    tags: list[str] = Field(default_factory=list)  # Extracted skills/keywords

    # Source
    source: JobSource
    source_url: str = ""

    # Timestamps
    posted_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None

    # Flags
    is_new: bool = True

    # Search — precomputed blob for full-text matching (computed at ETL time)
    search_blob: str = ""  # cleaned VN text of title+company+desc+reqs+tags

    @computed_field
    @property
    def computed_job_id(self) -> str:
        """Stable source URL identity; fallback includes location for URL-less jobs."""
        if self.job_id:
            return self.job_id
        url = urlsplit(self.source_url)
        if url.scheme in {"http", "https"} and url.netloc:
            query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True)
                     if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
            canonical = urlunsplit((url.scheme, url.netloc.lower(), url.path,
                                   urlencode(sorted(query)), ""))
            raw = f"{self.source.value}|{canonical}"
        else:
            raw = (f"{self.title_normalized}|{self.company.lower().strip()}|"
                   f"{self.location_normalized.lower().strip()}|{self.source.value}")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def generate_id(self) -> None:
        """Set job_id based on content hash."""
        self.job_id = self.computed_job_id

    def to_dynamo_item(self) -> dict:
        """Convert to DynamoDB item format."""
        item = {
            "job_id": self.job_id or self.computed_job_id,
            "source_posted": f"{self.source.value}#{self.scraped_at.strftime('%Y-%m-%d')}",
            "title": self.title,
            "title_normalized": self.title_normalized,
            "company": self.company,
            "location": self.location,
            "location_normalized": self.location_normalized,
            "salary_raw": self.salary_raw,
            "description": self.description[:2000],  # Limit for DynamoDB item size
            "job_type": self.job_type.value,
            "tags": self.tags,
            "source": self.source.value,
            "source_url": self.source_url,
            "scraped_at": self.scraped_at.isoformat(),
            "is_new": self.is_new,
        }

        # Optional fields
        if self.salary_min is not None:
            item["salary_min"] = self.salary_min
        if self.salary_max is not None:
            item["salary_max"] = self.salary_max
        if self.posted_at:
            item["posted_at"] = self.posted_at.isoformat()
        if self.expires_at:
            item["expires_at"] = self.expires_at.isoformat()
        if self.requirements:
            item["requirements"] = self.requirements[:2000]
        if self.search_blob:
            item["search_blob"] = self.search_blob[:4000]  # Limit for DynamoDB item size

        return item


# ── User & Subscription Models ───────────────────────────────────


class User(BaseModel):
    """Telegram bot user."""

    user_id: str  # Telegram chat_id
    username: str = ""
    first_name: str = ""
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_notified_at: Optional[datetime] = None

    def to_dynamo_item(self) -> dict:
        return {
            "user_id": self.user_id,
            "sk": "PROFILE",
            "username": self.username,
            "first_name": self.first_name,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "last_notified_at": self.last_notified_at.isoformat() if self.last_notified_at else "",
        }


class Subscription(BaseModel):
    """User keyword subscription for job alerts."""

    user_id: str
    keyword_raw: str  # Original input from user
    keyword_normalized: str = ""  # Lowered, trimmed, for matching
    location_filter: Optional[str] = None
    salary_min_filter: Optional[int] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_matched_at: Optional[datetime] = None
    match_count: int = 0

    def model_post_init(self, __context) -> None:
        if not self.keyword_normalized:
            self.keyword_normalized = self.keyword_raw.lower().strip()

    def to_dynamo_item(self) -> dict:
        return {
            "user_id": self.user_id,
            "sk": f"SUB#{self.keyword_normalized}",
            "keyword_raw": self.keyword_raw,
            "keyword_normalized": self.keyword_normalized,
            "location_filter": self.location_filter or "",
            "salary_min_filter": self.salary_min_filter or 0,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "last_matched_at": self.last_matched_at.isoformat() if self.last_matched_at else "",
            "match_count": self.match_count,
        }


# ── Notification Model ──────────────────────────────────────────


class JobMatch(BaseModel):
    """A matched job for notification."""

    job: Job
    subscription: Subscription
    match_score: float = 1.0  # 1.0 = exact match, <1.0 = fuzzy

    def format_telegram_message(self) -> str:
        """Format a single job match for Telegram."""
        salary = self.job.salary_raw if self.job.salary_raw else "Thỏa thuận"
        location = self.job.location if self.job.location else "Không rõ"
        source = self.job.source.value.capitalize()

        msg = (
            f"💼 *{_escape_md(self.job.title)}*\n"
            f"🏢 {_escape_md(self.job.company)}\n"
            f"📍 {_escape_md(location)}\n"
            f"💰 {_escape_md(salary)}\n"
            f"🔗 [{source}]({self.job.source_url})\n"
        )
        return msg


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    text = str(text or "").replace("\\", "\\\\")
    special_chars = r"_*[]()~`>#+-=|{}.!"
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text
