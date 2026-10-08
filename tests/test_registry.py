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
    assert set(registered) == set(JobSource)


def test_scheduled_run_includes_the_major_boards():
    settings = Settings(jooble_api_key=None)
    sources = {scraper.source for scraper in batch_scrapers(settings)}
    assert {
        JobSource.VIETNAMWORKS,
        JobSource.JOBSGO,
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
    assert len(live_sources(Settings(jooble_api_key=None))) == len(SOURCES) - 1


def test_source_label_is_human_readable():
    assert source_label("vieclam24h") == "ViecLam24h"
    assert source_label("chotot") == "Việc Làm Tốt"
    assert source_label("unknown-board") == "unknown-board"
