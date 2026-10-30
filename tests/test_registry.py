from src.common.models import JobSource
from src.config import Settings
from src.scrapers.registry import (
    SOURCES,
    available_sources,
    batch_scrapers,
    live_sources,
    source_label,
)


def test_every_job_source_is_registered_once():
    registered = [spec.source for spec in SOURCES]
    assert len(registered) == len(set(registered))
    # New JobSource values may exist before their adapter is registered (v2 rollout).
    assert set(registered) <= set(JobSource)


def test_scheduled_run_includes_the_major_boards():
    settings = Settings(jooble_api_key=None)
    sources = {scraper.source for scraper in batch_scrapers(settings)}
    assert {
        JobSource.VIETNAMWORKS,
        JobSource.INDEED,
        JobSource.TOPDEV,
        JobSource.CAREERVIET,
        JobSource.VIECLAM24H,
        JobSource.CAREERLINK,
        JobSource.ITVIEC,
        JobSource.CHOTOT,
    } <= sources
    assert JobSource.JOOBLE not in sources


def test_api_key_sources_appear_only_when_configured():
    without = {spec.source for spec in available_sources(Settings(jooble_api_key=None))}
    with_key = {spec.source for spec in available_sources(Settings(jooble_api_key="k"))}
    assert JobSource.JOOBLE not in without
    assert JobSource.JOOBLE in with_key


def test_live_sources_are_capped_and_ordered_by_priority():
    settings = Settings(jooble_api_key=None, live_search_max_sources=3)
    specs = live_sources(settings)
    assert [spec.source for spec in specs] == [
        JobSource.VIETNAMWORKS,
        JobSource.CAREERVIET,
        JobSource.VIECLAM24H,
    ]
    # Jooble needs a key; TopCV and LinkedIn are batch-only.
    batch_only = sum(1 for spec in SOURCES if not spec.live)
    settings = Settings(jooble_api_key=None)
    expected = min(len(SOURCES) - 1 - batch_only, settings.live_search_max_sources)
    assert len(live_sources(settings)) == expected


def test_topcv_runs_in_batch_only_when_you_search_is_enabled():
    off = {s.source for s in batch_scrapers(Settings(jooble_api_key=None))}
    on = {
        s.source
        for s in batch_scrapers(Settings(jooble_api_key=None, you_search_enabled=True))
    }
    assert JobSource.TOPCV not in off
    assert JobSource.TOPCV in on
    live = live_sources(Settings(jooble_api_key=None, you_search_enabled=True))
    assert JobSource.TOPCV not in {spec.source for spec in live}


def test_source_label_is_human_readable():
    assert source_label("vieclam24h") == "ViecLam24h"
    assert source_label("chotot") == "Việc Làm Tốt"
    assert source_label("unknown-board") == "unknown-board"


def test_batch_scrapers_carry_jsonld_fallback_where_verified():
    from src.scrapers.generic_jsonld_scraper import GenericJsonLdScraper
    from src.scrapers.registry import batch_scrapers

    by_source = {s.source: s for s in batch_scrapers(Settings(jooble_api_key=None))}
    assert isinstance(by_source[JobSource.CAREERVIET].fallback, GenericJsonLdScraper)
    assert by_source[JobSource.CAREERVIET].fallback.source is JobSource.CAREERVIET
    assert by_source[JobSource.VIETNAMWORKS].fallback is None
    assert isinstance(by_source[JobSource.VIECOI], GenericJsonLdScraper)


def test_jobsgo_does_not_impersonate_a_search_engine_crawler():
    assert "Googlebot" not in open(
        "src/scrapers/jobsgo_scraper.py", encoding="utf-8"
    ).read().split("class JobsGoScraper")[1]
