"""Budgeted detail-page fetch (src/etl/detail_fetch.py); the network is always mocked."""

import json
from unittest.mock import Mock

import pytest

from src.common.models import JobSource, RawJob
from src.etl import detail_fetch
from src.etl.detail_fetch import (
    DetailFetchConfig,
    DetailFetcher,
    fill_details,
    host_allowed,
    merge_details,
    needs_details,
)
from src.scrapers.generic_jsonld_scraper import parse_detail_html

LONG = "Mô tả chi tiết công việc kế toán tổng hợp, lập báo cáo thuế. " * 8
POSTING = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Kế toán tổng hợp",
    "description": f"<p>{LONG}</p>",
    "qualifications": "Ít nhất 2 năm kinh nghiệm, thành thạo MISA",
    "jobBenefits": "<ul><li>Lương tháng 13</li><li>BHXH đầy đủ</li></ul>",
    "validThrough": "2026-11-30",
    "datePosted": "2026-10-28",
    "hiringOrganization": {"name": "ACME"},
}
PAGE = (
    '<html><head><link rel="canonical" href="https://board.vn/job/1">'
    f'<script type="application/ld+json">{json.dumps(POSTING, ensure_ascii=False)}</script>'
    "</head></html>"
)


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def fake_session(status=200, body=PAGE, error=None):
    session = Mock()

    def get(url, timeout, allow_redirects):
        assert timeout == 5.0
        if error:
            raise error
        return Mock(status_code=status, content=body.encode("utf-8"), url=url)

    session.get = Mock(side_effect=get)
    return session


def make_fetcher(session=None, clock=None, **config):
    clock = clock or FakeClock()
    return DetailFetcher(config=DetailFetchConfig(**config), session=session or fake_session(),
                         clock=clock, sleep=clock.sleep)


def raw(url="https://board.vn/job/1", description="Kế toán", **changes):
    return RawJob(title="Kế toán", source=JobSource.CAREERLINK, source_url=url,
                  description=description, **changes)


def test_parse_detail_html_reads_jsonld_including_benefits_and_deadline():
    fields = parse_detail_html(PAGE, "https://board.vn/job/1?utm_source=x")
    assert fields["has_job_posting"] is True
    assert fields["company"] == "ACME"
    assert fields["valid_through_raw"] == "2026-11-30"
    assert "Lương tháng 13" in fields["benefits"]
    assert "MISA" in fields["requirements"]
    assert fields["description"].startswith("Mô tả chi tiết")
    assert fields["source_url"] == "https://board.vn/job/1"


def test_parse_detail_html_falls_back_to_opengraph_or_nothing():
    page = '<meta property="og:title" content="Kế toán"><meta name="description" content="Mô tả">'
    assert parse_detail_html(page, "https://x.vn/1") == {
        "title": "Kế toán", "description": "Mô tả", "has_job_posting": False,
        "source_url": "https://x.vn/1",
    }
    assert parse_detail_html("<html></html>", "https://x.vn/1") == {}


def test_needs_details_and_host_rules():
    assert needs_details(raw())  # short description
    assert needs_details(raw(description=LONG))  # no deadline
    assert not needs_details(raw(description=LONG, valid_through_raw="2026-11-30"))
    assert host_allowed("https://www.careerlink.vn/tim-viec-lam/x/1")
    for blocked in ("https://jobsgo.vn/viec-lam/1", "https://www.topcv.vn/viec-lam/1",
                    "ftp://board.vn/1", ""):
        assert not host_allowed(blocked)


def test_merge_details_only_improves_fields():
    merged = merge_details(raw(salary_raw="10 triệu"), parse_detail_html(PAGE, ""))
    assert merged.description.startswith("Mô tả chi tiết")
    assert merged.valid_through_raw == "2026-11-30"
    assert merged.salary_raw == "10 triệu"
    assert "BHXH" in merged.benefits_raw
    same = raw(description=LONG + LONG)
    assert merge_details(same, {"description": "ngắn"}).description == same.description


def test_fetcher_rate_limits_and_caps_pages():
    clock = FakeClock()
    fetcher = make_fetcher(clock=clock, max_pages=2, budget_seconds=60)
    assert fetcher.fetch("https://board.vn/job/1")["company"] == "ACME"
    assert fetcher.fetch("https://board.vn/job/2") is not None
    assert fetcher.fetch("https://board.vn/job/3") is None  # page budget spent
    assert clock.sleeps == [1.0]
    assert fetcher.session.get.call_count == 2


def test_fetcher_stops_on_wall_budget_and_lambda_time():
    clock = FakeClock()
    fetcher = make_fetcher(clock=clock, budget_seconds=8)
    fetcher.fetch("https://board.vn/job/1")
    clock.now += 3  # 3 s used + 1 s spacing + 5 s timeout > 8 s
    assert fetcher.fetch("https://board.vn/job/2") is None
    late = make_fetcher()
    late.remaining_ms = lambda: 9000  # < 8 s reserve + 5 s timeout
    assert late.fetch("https://board.vn/job/1") is None
    assert late.session.get.call_count == 0


def test_blocked_host_is_skipped_for_the_rest_of_the_run():
    fetcher = make_fetcher(session=fake_session(status=403), max_pages=5)
    assert fetcher.fetch("https://board.vn/job/1") is None
    assert fetcher.fetch("https://board.vn/job/2") is None
    assert fetcher.session.get.call_count == 1
    assert not fetcher.can_fetch("https://board.vn/job/3")


def test_network_errors_never_raise():
    fetcher = make_fetcher(session=fake_session(error=TimeoutError("slow")))
    assert fetcher.fetch("https://board.vn/job/1") is None
    assert fetcher.failures == 1


def test_fill_details_reuses_stored_details_before_fetching():
    fetcher = make_fetcher()
    stored = {"description": LONG, "expires_at": "2026-11-30T16:59:59+00:00",
              "benefits": ["13th_month"], "details_fetched_at": "2026-10-29T00:00:00+00:00"}
    jobs = [raw("https://board.vn/job/1"), raw("https://board.vn/job/2"),
            raw("https://jobsgo.vn/job/3")]
    lookup = Mock(side_effect=lambda job_id: stored if job_id == "a" else None)
    filled, info, stats = fill_details(jobs, ["a", "b", "c"], fetcher, lookup=lookup)
    assert stats == {"candidates": 2, "reused": 1, "fetched": 1, "failed": 0, "skipped": 1}
    assert filled[0].description == LONG and filled[0].valid_through_raw.startswith("2026-11-30")
    assert info[0] == {"details_fetched_at": stored["details_fetched_at"],
                       "benefits": ["13th_month"]}
    assert filled[1].valid_through_raw == "2026-11-30"
    assert filled[2] is jobs[2]
    assert fetcher.session.get.call_count == 1


def test_config_reads_environment(monkeypatch):
    monkeypatch.setenv("VIECLAMBOT_DETAIL_FETCH_PER_BATCH", "3")
    monkeypatch.setenv("VIECLAMBOT_DETAIL_FETCH_SKIP_HOSTS", "a.vn, b.vn")
    monkeypatch.setenv("VIECLAMBOT_DETAIL_FETCH_TIMEOUT_SECONDS", "bad")
    config = DetailFetchConfig.from_env()
    assert config.max_pages == 3 and config.skip_hosts == ("a.vn", "b.vn")
    assert config.timeout == 5.0
    assert config.enabled is False  # disabled by default under pytest
    monkeypatch.setenv("VIECLAMBOT_DETAIL_FETCH_ENABLED", "true")
    assert DetailFetchConfig.from_env().enabled is True


@pytest.fixture
def etl_with_fake_pages(monkeypatch):
    from lambdas import etl_handler

    session = fake_session()
    monkeypatch.setenv("VIECLAMBOT_DETAIL_FETCH_ENABLED", "true")
    monkeypatch.setenv("VIECLAMBOT_DETAIL_FETCH_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(detail_fetch.DetailFetcher, "_session", lambda self: session)
    monkeypatch.setattr(etl_handler.S3Loader, "save_processed_data", Mock(return_value="k"))
    monkeypatch.setattr(etl_handler, "save_processed_ndjson", Mock(return_value="k"))
    return etl_handler.handler, session


def test_etl_fills_thin_job_once_and_reuses_it_on_rescrape(database, etl_with_fake_pages):
    handler, session = etl_with_fake_pages
    record = {"body": json.dumps({"title": "Kế toán", "company": "ACME", "source": "careerlink",
                                  "source_url": "https://board.vn/job/1",
                                  "description": "Kế toán"})}
    context = Mock(get_remaining_time_in_millis=Mock(return_value=25_000))
    summary = handler({"Records": [record]}, context)
    assert summary["details"]["fetched"] == 1
    item = database[0].scan()["Items"][0]
    assert item["description"].startswith("Mô tả chi tiết")
    assert item["expires_at"].startswith("2026-11-30")
    assert "13th_month" in item["benefits"] and item["details_fetched_at"]
    assert item["experience_min_years"] == 2

    summary = handler({"Records": [record]}, context)
    assert summary["details"]["reused"] == 1 and summary["details"]["fetched"] == 0
    assert session.get.call_count == 1
    item = database[0].scan()["Items"][0]
    assert item["description"].startswith("Mô tả chi tiết")  # not reverted to the card text
    assert "13th_month" in item["benefits"]
