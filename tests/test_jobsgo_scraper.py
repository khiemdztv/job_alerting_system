"""JobsGO scraper parsing, URL canonicalisation and layout-change signal."""

from pathlib import Path
from unittest.mock import Mock

from src.common.models import JobSource
from src.scrapers.jobsgo_scraper import JobsGoScraper

FIXTURE = Path(__file__).parent / "fixtures" / "jobsgo" / "search.html"


def html_response(text: str) -> Mock:
    response = Mock()
    response.text = text
    return response


def scraper_with(*texts: str) -> tuple[JobsGoScraper, Mock]:
    scraper = JobsGoScraper()
    get = Mock(side_effect=[html_response(text) for text in texts])
    scraper._get = get
    return scraper, get


def test_parses_cards_and_strips_tracking_params():
    scraper, get = scraper_with(FIXTURE.read_text(encoding="utf-8"))

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert get.call_args.args[0] == "https://jobsgo.vn/viec-lam-ke-toan.html"
    assert len(jobs) == 2
    first, second = jobs
    assert first.title == "Kế Toán Tổng Hợp Tại Hồ Chí Minh"
    assert first.company == "Công ty TNHH ABC"
    assert first.location == "Hà Nội"
    assert first.salary_raw == "12 - 15 triệu VNĐ"
    assert first.job_type_raw == "Full-time"
    assert first.posted_at_raw == "2 giờ trước"
    assert first.source is JobSource.JOBSGO
    assert first.source_url == "https://jobsgo.vn/viec-lam/ke-toan-tong-hop-101.html"
    assert second.job_type_raw == "Thực tập"
    assert second.location == "Đà Nẵng"
    assert scraper.last_error == ""


def test_stops_when_a_page_repeats_and_in_live_mode():
    page = FIXTURE.read_text(encoding="utf-8")
    scraper, get = scraper_with(page, page)

    assert len(scraper.scrape("kế toán", max_pages=3)) == 2
    assert get.call_count == 2

    live, live_get = scraper_with(page, page)
    live.use_live_budget(None, 4.0)
    live.scrape("kế toán", max_pages=3)
    assert live_get.call_count == 1


def test_page_without_cards_flags_layout_change():
    scraper, _ = scraper_with("<html><body><div class='new-layout'>x</div></body></html>")

    assert scraper.scrape("kế toán", max_pages=1) == []
    assert scraper.last_error == "layout_changed"


def test_no_result_page_is_not_a_layout_change():
    scraper, _ = scraper_with("<html><body><p>Không tìm thấy việc làm phù hợp</p></body></html>")

    assert scraper.scrape("zzqx", max_pages=1) == []
    assert scraper.last_error == ""
