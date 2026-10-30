from pathlib import Path
from unittest.mock import Mock

from bs4 import BeautifulSoup

from src.common.models import JobSource
from src.scrapers.job123_scraper import Job123Scraper

FIXTURE = Path(__file__).parent / "fixtures" / "job123" / "tuyen_dung_ke_toan.html"


def response(text):
    result = Mock()
    result.text = text
    return result


def make(monkeypatch, html):
    scraper = Job123Scraper()
    get = Mock(return_value=response(html))
    monkeypatch.setattr(scraper, "_get", get)
    return scraper, get


def test_parses_real_fixture_cards(monkeypatch):
    scraper, get = make(monkeypatch, FIXTURE.read_text(encoding="utf-8"))

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert len(jobs) == 6
    first = jobs[0]
    assert first.source == JobSource.JOB123
    assert first.title == "Kế Toán Tổng Hợp"
    assert first.company.startswith("Công Ty Cổ Phần Liên Doanh Cảng")
    assert first.location == "Quảng Trị"
    assert first.salary_raw == ""  # "__" placeholder is not a salary
    assert first.posted_at_raw == "2 tháng trước"
    assert first.source_url == "https://123job.vn/viec-lam/ke-toan-tong-hop-JDaj4kwX9G"
    assert jobs[1].salary_raw == "7 - 10 triệu"
    assert jobs[1].location == "Quảng Ngãi"
    assert get.call_args.kwargs["params"] == {"q": "kế toán"}


def test_pagination_and_latest_use_expected_params(monkeypatch):
    scraper, get = make(monkeypatch, FIXTURE.read_text(encoding="utf-8"))

    jobs = scraper.scrape("kế toán", max_pages=2)

    assert [c.kwargs["params"] for c in get.call_args_list] == [
        {"q": "kế toán"},
        {"q": "kế toán", "page": 2},
    ]
    assert len(jobs) == 6  # page 2 repeats the same urls: de-duplicated

    scraper2, get2 = make(monkeypatch, FIXTURE.read_text(encoding="utf-8"))
    scraper2.scrape_latest(max_pages=1)
    assert get2.call_args.args[0] == "https://123job.vn/tuyen-dung"
    assert get2.call_args.kwargs["params"] is None


def test_location_is_post_filter_and_live_mode_is_single_request(monkeypatch):
    scraper, get = make(monkeypatch, FIXTURE.read_text(encoding="utf-8"))
    scraper.live_mode = True

    jobs = scraper.scrape("kế toán", max_pages=5, location="Hải Phòng")

    assert get.call_count == 1
    assert [j.location for j in jobs] == ["Hải Phòng", "Hải Phòng"]


def test_zero_cards_marks_layout_changed(monkeypatch):
    scraper, _ = make(monkeypatch, "<html><body><p>Không có dữ liệu</p></body></html>")

    assert scraper.scrape("kế toán", max_pages=1) == []
    assert scraper.last_error == "layout_changed"


def test_parse_job_ignores_non_job_markup():
    node = BeautifulSoup("<div><a href='/about'>About</a></div>", "html.parser").div
    assert Job123Scraper().parse_job(node) is None
