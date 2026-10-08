"""Single registry of every job source and where it is used.

The scheduled scraper, the interactive live-board search, and the diagnostics
scripts used to keep three different hand-written lists of scrapers, so new
boards were silently missing from one of them.  Register a source once here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.common.models import JobSource
from src.config import Settings, get_settings
from src.scrapers.base_scraper import BaseScraper
from src.scrapers.careerlink_scraper import CareerLinkScraper
from src.scrapers.careerviet_scraper import CareerVietScraper
from src.scrapers.chotot_scraper import ChototScraper
from src.scrapers.itviec_scraper import ITviecScraper
from src.scrapers.jobsgo_scraper import JobsGoScraper
from src.scrapers.jooble_scraper import JoobleScraper
from src.scrapers.timviec365_scraper import TimViec365Scraper
from src.scrapers.topdev_scraper import TopDevScraper
from src.scrapers.vieclam24h_scraper import ViecLam24hScraper
from src.scrapers.vietnamworks_scraper import VietnamWorksScraper
from src.scrapers.ybox_scraper import YBoxScraper


@dataclass(frozen=True)
class SourceSpec:
    """How one job board participates in the system."""

    source: JobSource
    label: str
    factory: Callable[[], BaseScraper]
    batch: bool = True
    """Included in the scheduled scraper run that fills DynamoDB."""
    live: bool = True
    """Queried directly during an interactive /search (must answer in seconds)."""
    live_priority: int = 50
    """Lower runs first when the live budget allows fewer sources than registered."""
    requires_setting: str | None = None
    """Settings attribute that must be truthy (API key) for the source to run."""
    live_timeout: float | None = None
    """Per-request timeout override for live mode (defaults to settings)."""

    def is_available(self, settings: Settings) -> bool:
        if self.requires_setting is None:
            return True
        return bool(getattr(settings, self.requires_setting, None))


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(JobSource.VIETNAMWORKS, "VietnamWorks", VietnamWorksScraper, live_priority=10),
    SourceSpec(JobSource.CAREERVIET, "CareerViet", CareerVietScraper, live_priority=11),
    SourceSpec(JobSource.VIECLAM24H, "ViecLam24h", ViecLam24hScraper, live_priority=12),
    SourceSpec(JobSource.JOBSGO, "JobsGO", JobsGoScraper, live_priority=13),
    SourceSpec(JobSource.CAREERLINK, "CareerLink", CareerLinkScraper, live_priority=14),
    SourceSpec(JobSource.ITVIEC, "ITviec", ITviecScraper, live_priority=15),
    SourceSpec(JobSource.CHOTOT, "Việc Làm Tốt", ChototScraper, live_priority=16),
    SourceSpec(JobSource.TIMVIEC365, "TimViec365", TimViec365Scraper, live_priority=17),
    SourceSpec(
        JobSource.JOOBLE, "Jooble", JoobleScraper, live_priority=18,
        requires_setting="jooble_api_key",
    ),
    # TopDev answers in ~2s when healthy but its busy server regularly exceeds
    # 8s; a tight live timeout keeps one slow board from stretching every search.
    SourceSpec(JobSource.TOPDEV, "TopDev", TopDevScraper, live_priority=30, live_timeout=4.0),
    # YBox downloads one big listing page and filters locally; fine for batch,
    # and cheap enough for live because it is a single request.
    SourceSpec(JobSource.YBOX, "YBox", YBoxScraper, live_priority=40),
)

SOURCE_LABELS: dict[str, str] = {spec.source.value: spec.label for spec in SOURCES}


def source_label(value: str) -> str:
    """Human-readable board name for a JobSource value (falls back to the value)."""
    return SOURCE_LABELS.get(str(value), str(value))


def available_sources(settings: Settings | None = None) -> list[SourceSpec]:
    settings = settings or get_settings()
    return [spec for spec in SOURCES if spec.is_available(settings)]


def batch_scrapers(settings: Settings | None = None) -> list[BaseScraper]:
    """Scrapers for the scheduled run; every available board participates."""
    return [spec.factory() for spec in available_sources(settings) if spec.batch]


def live_sources(settings: Settings | None = None) -> list[SourceSpec]:
    """Boards queried during an interactive search, best first, capped by settings."""
    settings = settings or get_settings()
    specs = sorted(
        (spec for spec in available_sources(settings) if spec.live),
        key=lambda spec: spec.live_priority,
    )
    return specs[: settings.live_search_max_sources]
