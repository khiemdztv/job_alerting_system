import time
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.common.models import JobSource, RawJob
from src.config import Settings
from src.scrapers.base_scraper import BaseScraper
from src.scrapers.registry import SourceSpec
from src.web_search import live_boards
from src.web_search.live_boards import clear_live_cache, search_live_job_boards


def raw(title, company, location, source, url):
    return RawJob(
        title=title,
        company=company,
        location=location,
        source=source,
        source_url=url,
        scraped_at=datetime.now(timezone.utc),
    )


class FakeScraper(BaseScraper):
    """Scraper double that records live-mode configuration and returns canned jobs."""

    def __init__(self, source, jobs=(), *, delay=0.0, error=None):
        super().__init__(source=source)
        self._jobs, self._delay, self._error = list(jobs), delay, error
        self.calls = []

    def scrape(self, keyword, max_pages=None):
        self.calls.append((keyword, max_pages))
        if self._delay:
            time.sleep(self._delay)
        if self._error:
            raise self._error
        return list(self._jobs)

    def parse_job(self, raw_data):  # pragma: no cover - not used
        return None


def spec(source, scraper, **changes):
    return SourceSpec(source, source.value, lambda: scraper, **changes)


@pytest.fixture(autouse=True)
def fresh_cache():
    clear_live_cache()
    yield
    clear_live_cache()


def test_live_boards_find_active_related_ai_internships(monkeypatch):
    vieclam = FakeScraper(
        JobSource.VIECLAM24H,
        [
            raw(
                "Intern AI - ERP - Software Developer",
                "T4 Tek",
                "TP.HCM",
                JobSource.VIECLAM24H,
                "https://vieclam24h.vn/job/1?search_id=tracking&open_from=list",
            ),
            raw(
                "AI Engineer Intern",
                "Wrong City",
                "Hà Nội",
                JobSource.VIECLAM24H,
                "https://vieclam24h.vn/job/2",
            ),
        ],
    )
    careerviet = FakeScraper(
        JobSource.CAREERVIET,
        [
            raw(
                "Project cum AI Intern",
                "CareerViet",
                "Hồ Chí Minh",
                JobSource.CAREERVIET,
                "https://careerviet.vn/vi/tim-viec-lam/project-ai.1.html",
            )
        ],
    )
    jobsgo = FakeScraper(JobSource.JOBSGO)
    monkeypatch.setattr(
        live_boards,
        "live_sources",
        lambda settings: [
            spec(JobSource.VIECLAM24H, vieclam),
            spec(JobSource.CAREERVIET, careerviet),
            spec(JobSource.JOBSGO, jobsgo),
        ],
    )

    jobs = search_live_job_boards("ai engineer intern", location="HCM", limit=10)

    assert [job["title"] for job in jobs] == [
        "Project cum AI Intern",
        "Intern AI - ERP - Software Developer",
    ]
    assert jobs[1]["source_url"] == "https://vieclam24h.vn/job/1"
    assert jobs[0]["source"] == "Live · CareerViet"
    assert all(job["is_live_listing"] for job in jobs)
    # Boards receive the broadened internship wording, one page, in live mode.
    assert careerviet.calls == [("ai intern", 1)]
    assert vieclam.calls == [("ai engineer intern", 1)]
    assert careerviet.live_mode and careerviet.budget_margin < 5


def test_every_registered_live_board_is_queried_in_parallel(monkeypatch):
    scrapers = [
        FakeScraper(source, [raw(f"Kế toán {source.value}", "Cty", "Hà Nội", source,
                                 f"https://{source.value}.example/{i}")], delay=0.3)
        for i, source in enumerate(
            (JobSource.CAREERLINK, JobSource.ITVIEC, JobSource.CHOTOT, JobSource.JOBSGO)
        )
    ]
    monkeypatch.setattr(
        live_boards, "live_sources", lambda settings: [spec(s.source, s) for s in scrapers]
    )
    started = time.monotonic()
    stats = {}
    jobs = search_live_job_boards("kế toán", location="Hà Nội", limit=20, stats=stats)
    elapsed = time.monotonic() - started

    assert len(jobs) == 4
    assert {job["source"] for job in jobs} == {
        "Live · CareerLink", "Live · ITviec", "Live · Việc Làm Tốt", "Live · JobsGO",
    }
    assert elapsed < 1.0, "boards must be scraped concurrently, not one after another"
    assert all(stats[s.source.value]["jobs"] == 1 for s in scrapers)
    assert all(stats[s.source.value]["relevant"] == 1 for s in scrapers)


def test_slow_board_does_not_block_the_others_past_the_deadline(monkeypatch):
    fast = FakeScraper(
        JobSource.CAREERVIET,
        [raw("Kế toán tổng hợp", "Cty A", "Hồ Chí Minh", JobSource.CAREERVIET,
             "https://careerviet.vn/a")],
    )
    slow = FakeScraper(JobSource.TOPDEV, [raw("Kế toán", "Cty B", "Hồ Chí Minh",
                                              JobSource.TOPDEV, "https://topdev.vn/b")],
                       delay=4.0)
    broken = FakeScraper(JobSource.ITVIEC, error=RuntimeError("layout changed"))
    monkeypatch.setattr(
        live_boards,
        "live_sources",
        lambda settings: [
            spec(JobSource.CAREERVIET, fast),
            spec(JobSource.TOPDEV, slow),
            spec(JobSource.ITVIEC, broken),
        ],
    )
    stats = {}
    started = time.monotonic()
    jobs = search_live_job_boards(
        "kế toán", limit=10, deadline_at=time.monotonic() + 2.0, stats=stats
    )

    assert time.monotonic() - started < 3.5
    assert [job["title"] for job in jobs] == ["Kế toán tổng hợp"]
    assert stats["topdev"]["error"] == "DeadlineExceeded"
    assert stats["itviec"]["error"] == "RuntimeError"
    assert stats["careerviet"]["jobs"] == 1


def test_repeated_search_is_served_from_cache(monkeypatch):
    scraper = FakeScraper(
        JobSource.JOBSGO,
        [raw("Nhân viên kế toán", "Cty", "Hà Nội", JobSource.JOBSGO, "https://jobsgo.vn/1")],
    )
    monkeypatch.setattr(live_boards, "live_sources", lambda settings: [spec(JobSource.JOBSGO, scraper)])
    settings = Settings(live_search_cache_seconds=60)

    first = search_live_job_boards("kế toán", limit=10, settings=settings)
    second = search_live_job_boards("Kế Toán", limit=10, settings=settings)

    assert first == second
    assert len(scraper.calls) == 1


def test_live_search_can_be_limited_to_top_priority_boards():
    settings = Settings(jooble_api_key=None, live_search_max_sources=2)
    from src.scrapers.registry import live_sources

    assert [s.source for s in live_sources(settings)] == [
        JobSource.VIETNAMWORKS, JobSource.CAREERVIET,
    ]


def test_live_mode_uses_short_timeouts_and_no_retries():
    scraper = FakeScraper(JobSource.CAREERLINK)
    scraper.use_live_budget(time.monotonic() + 10, 4.0)
    adapter = scraper.session.get_adapter("https://www.careerlink.vn/")

    assert scraper.request_timeout == 4.0
    assert adapter.max_retries.total == 0
    assert scraper.live_mode

    sent = Mock(return_value=Mock(raise_for_status=Mock()))
    scraper.session.get = sent
    scraper._get("https://www.careerlink.vn/vieclam/list", timeout=30)
    assert sent.call_args.kwargs["timeout"] == 4.0
