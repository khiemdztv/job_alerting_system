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
from src.scrapers.generic_jsonld_scraper import KNOWN_SITES, GenericJsonLdScraper
from src.scrapers.indeed_scraper import IndeedScraper
from src.scrapers.itviec_scraper import ITviecScraper
from src.scrapers.job123_scraper import Job123Scraper
from src.scrapers.joboko_scraper import JobokoScraper
from src.scrapers.jobsgo_scraper import JobsGoScraper
from src.scrapers.jooble_scraper import JoobleScraper
from src.scrapers.linkedin_scraper import LinkedInScraper
from src.scrapers.timviec365_scraper import TimViec365Scraper
from src.scrapers.topcv_scraper import TopCVScraper
from src.scrapers.topdev_scraper import TopDevScraper
from src.scrapers.vieclam24h_scraper import ViecLam24hScraper
from src.scrapers.vieclam_gov_scraper import VieclamGovScraper
from src.scrapers.vietnamworks_scraper import VietnamWorksScraper
from src.scrapers.ybox_scraper import YBoxScraper

_KNOWN_SITE = {site.source: site for site in KNOWN_SITES}


def jsonld_scraper(source: JobSource) -> Callable[[], BaseScraper]:
    """Factory for the sitemap + JSON-LD scraper of a board verified in KNOWN_SITES."""
    site = _KNOWN_SITE[source]
    return lambda: GenericJsonLdScraper(site)


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
    fallback_factory: Callable[[], BaseScraper] | None = None
    """Selector-free scraper used when the dedicated parser returns no jobs."""

    def is_available(self, settings: Settings) -> bool:
        if self.requires_setting is None:
            return True
        return bool(getattr(settings, self.requires_setting, None))


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(JobSource.VIETNAMWORKS, "VietnamWorks", VietnamWorksScraper, live_priority=10),
    SourceSpec(
        JobSource.CAREERVIET, "CareerViet", CareerVietScraper, live_priority=11,
        fallback_factory=jsonld_scraper(JobSource.CAREERVIET),
    ),
    SourceSpec(
        JobSource.VIECLAM24H, "ViecLam24h", ViecLam24hScraper, live_priority=12,
        live_timeout=5.0,  # ~1 MB pages, 1.6-3.3s
        fallback_factory=jsonld_scraper(JobSource.VIECLAM24H),
    ),
    # JobsGO sometimes answers plain clients with a Cloudflare challenge (403).
    # The old scraper impersonated Googlebot to get through; that was removed.
    # A probe on 30/10/2026 with the normal browser User-Agent returned 50 jobs
    # in 1.8s, so it stays enabled; a challenge is recorded as "blocked".
    SourceSpec(JobSource.JOBSGO, "JobsGO", JobsGoScraper, live_priority=13),
    SourceSpec(
        JobSource.CAREERLINK, "CareerLink", CareerLinkScraper, live_priority=14,
        fallback_factory=jsonld_scraper(JobSource.CAREERLINK),
    ),
    SourceSpec(
        JobSource.ITVIEC, "ITviec", ITviecScraper, live_priority=15,
        fallback_factory=jsonld_scraper(JobSource.ITVIEC),
    ),
    SourceSpec(JobSource.CHOTOT, "Việc Làm Tốt", ChototScraper, live_priority=16),
    # TimViec365 category pages answer in 2–4s, city pages up to ~6.5s.
    SourceSpec(
        JobSource.TIMVIEC365, "TimViec365", TimViec365Scraper, live_priority=17,
        live_timeout=7.0,
    ),
    SourceSpec(
        JobSource.JOOBLE, "Jooble", JoobleScraper, live_priority=18,
        requires_setting="jooble_api_key",
    ),
    # TopDev answers in ~2s when healthy but its busy server regularly exceeds
    # 8s; a tight live timeout keeps one slow board from stretching every search.
    # TopDev now uses its JSON API (1–2.5s, occasionally ~6s).
    SourceSpec(JobSource.TOPDEV, "TopDev", TopDevScraper, live_priority=30, live_timeout=6.0),
    # Indeed VN: one public GraphQL POST returns up to 100 postings with salary,
    # description and posted date; many are re-listings of local boards, so it
    # ranks after them in live mode.  Terms forbid scraping: keep it low volume.
    SourceSpec(JobSource.INDEED, "Indeed", IndeedScraper, live_priority=20, live_timeout=8.0),
    # LinkedIn guest search: 10 cards per page, no salary, throttles after a
    # few requests.  Batch only, as a best-effort extra source.
    SourceSpec(JobSource.LINKEDIN, "LinkedIn", LinkedInScraper, live=False),
    # Server-rendered Vietnamese boards verified 30/10/2026 (docs/SOURCES_RESEARCH_VN.md).
    SourceSpec(
        JobSource.JOB123, "123job", Job123Scraper, live_priority=20, live_timeout=5.0,
        fallback_factory=jsonld_scraper(JobSource.JOB123),
    ),
    SourceSpec(JobSource.JOBOKO, "JobOKO", JobokoScraper, live_priority=25, live_timeout=6.0),
    SourceSpec(
        JobSource.VIECLAM_GOV, "Vieclam.gov.vn", VieclamGovScraper,
        live_priority=40, live_timeout=5.0,
    ),
    # Viecoi: sitemap + JSON-LD only; its sitemap takes ~25s, so batch only.
    SourceSpec(JobSource.VIECOI, "Viecoi", jsonld_scraper(JobSource.VIECOI), live=False),
    # YBox downloads one big listing page and filters locally; fine for batch,
    # and cheap enough for live because it is a single request.
    SourceSpec(JobSource.YBOX, "YBox", YBoxScraper, live_priority=40),
    # TopCV blocks direct requests; it is read from the You.com index instead.
    # Not live: /search already queries You.com across all job domains.
    SourceSpec(
        JobSource.TOPCV, "TopCV", TopCVScraper, live=False,
        requires_setting="you_search_enabled",
    ),
)

SOURCE_LABELS: dict[str, str] = {spec.source.value: spec.label for spec in SOURCES}


def _attach_fallback(spec: SourceSpec) -> BaseScraper:
    scraper = spec.factory()
    if spec.fallback_factory is not None:
        scraper.fallback = spec.fallback_factory()
    return scraper


def source_label(value: str) -> str:
    """Human-readable board name for a JobSource value (falls back to the value)."""
    return SOURCE_LABELS.get(str(value), str(value))


def available_sources(settings: Settings | None = None) -> list[SourceSpec]:
    settings = settings or get_settings()
    return [spec for spec in SOURCES if spec.is_available(settings)]


def batch_scrapers(settings: Settings | None = None) -> list[BaseScraper]:
    """Scrapers for the scheduled run; every available board participates."""
    return [_attach_fallback(spec) for spec in available_sources(settings) if spec.batch]


def live_sources(settings: Settings | None = None) -> list[SourceSpec]:
    """Boards queried during an interactive search, best first, capped by settings."""
    settings = settings or get_settings()
    specs = sorted(
        (spec for spec in available_sources(settings) if spec.live),
        key=lambda spec: spec.live_priority,
    )
    return specs[: settings.live_search_max_sources]
