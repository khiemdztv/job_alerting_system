"""
Centralized configuration for ViecLamBot.
Uses pydantic-settings for environment variable parsing with validation.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_prefix": "VIECLAMBOT_", "env_file": ".env", "extra": "ignore"}

    # ── AWS ──────────────────────────────────────────────────────
    aws_region: str = Field(default="ap-southeast-1", description="AWS region")
    dynamodb_jobs_table: str = Field(default="vieclambot-jobs")
    dynamodb_users_table: str = Field(default="vieclambot-users")
    dynamodb_subscriptions_table: str = Field(default="vieclambot-subscriptions")
    s3_data_lake_bucket: str = Field(default="vieclambot-data-lake")
    sqs_raw_jobs_queue: str = Field(default="vieclambot-raw-jobs")

    # ── Scrapers ─────────────────────────────────────────────────
    jooble_api_key: Optional[str] = Field(default=None, description="Jooble API key (free)")
    scrape_delay_seconds: float = Field(default=2.0, description="Delay between requests")
    scrape_max_pages: int = Field(default=5, description="Max pages per source per run")
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    )

    # ── Telegram ─────────────────────────────────────────────────
    telegram_bot_token: Optional[str] = Field(default=None)
    telegram_webhook_url: Optional[str] = Field(default=None)
    admin_chat_id: Optional[str] = Field(
        default=None,
        description="Telegram chat ID for admin alerts (scraper health)",
    )

    # ── Scheduler ────────────────────────────────────────────────
    alert_interval_hours: int = Field(default=6, description="Hours between alerts")
    alert_recovery_days: int = Field(default=7, ge=1, le=60)
    alert_max_jobs_per_user: int = Field(default=15, ge=1, le=50)
    search_result_limit: int = Field(default=100, ge=10, le=200)
    search_snapshot_cache_seconds: int = Field(default=300, ge=0, le=3600)
    max_subscriptions: int = Field(default=10, ge=1, le=50)
    scrape_workers: int = Field(default=4, ge=1, le=8)
    alert_cron_expressions: list[str] = Field(
        default=["cron(0 0 * * ? *)", "cron(0 6 * * ? *)",
                 "cron(0 12 * * ? *)", "cron(0 18 * * ? *)"],
        description="EventBridge cron expressions (UTC) for alert schedule",
    )

    # ── Data Quality ─────────────────────────────────────────────
    min_title_length: int = Field(default=3)
    max_job_age_days: int = Field(default=60, description="TTL for jobs in DynamoDB")

    # ── Logging ──────────────────────────────────────────────────
    log_level: str = Field(default="INFO")

    @property
    def scraper_headers(self) -> dict[str, str]:
        """Standard headers for HTTP requests."""
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
