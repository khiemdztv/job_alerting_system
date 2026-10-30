"""TimViec365 scraper against trimmed real pages captured on 30 Oct 2026."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.common.models import JobSource
from src.scrapers.timviec365_scraper import (
    BASE_URL,
    LATEST_URL,
    TimViec365Scraper,
    listing_url,
    match_category,
)

FIXTURES = Path(__file__).parent / "fixtures" / "timviec365"


def page(name: str) -> Mock:
    response = Mock()
    response.text = (FIXTURES / name).read_text(encoding="utf-8")
    return response


def scraper_with(*pages: str) -> tuple[TimViec365Scraper, Mock]:
    scraper = TimViec365Scraper()
    get = Mock(side_effect=[page(name) for name in pages])
    scraper._get = get
    return scraper, get


def test_parses_category_cards():
    scraper, _ = scraper_with("category_ha_noi.html")

    jobs = scraper.scrape("kế toán", max_pages=1, location="Hà Nội")

    assert len(jobs) == 3
    pinned, regular = jobs[0], jobs[1]
    assert pinned.title == "TUYỂN KẾ TOÁN TỔNG HỢP LƯƠNG 16-20 TRIỆU"
    assert pinned.company == "CÔNG TY TNHH ANKA TB"
    assert pinned.location == "Hà Nội"
    assert pinned.salary_raw == "Từ 16.000.000 VNĐ Đến 20.000.000 VNĐ"
    assert pinned.source is JobSource.TIMVIEC365
    assert pinned.source_url == (
        f"{BASE_URL}/tuyen-ke-toan-tong-hop-luong-1620-trieu-p2071005.html"
    )
    # Pinned ads carry no age; the deadline must not be mistaken for a posting date.
    assert pinned.posted_at_raw == ""
    assert "Hạn nộp: 30/11/2026" in pinned.description
    assert regular.posted_at_raw == "5 giờ trước"
    assert scraper.last_error == ""


def test_keyword_maps_to_category_and_city_path():
    scraper, get = scraper_with("category_ha_noi.html")

    scraper.scrape("kế toán", max_pages=1, location="Hà Nội")

    assert get.call_args.args[0] == f"{BASE_URL}/viec-lam-ke-toan-kiem-toan-tai-ha-noi-c1v1"
    assert get.call_args.kwargs["params"] is None


def test_never_uses_robots_disallowed_search():
    scraper, get = scraper_with("latest.html")

    scraper.scrape("qwerty unmatched words", max_pages=1)

    assert get.call_args.args[0] == LATEST_URL
    assert "/tim-kiem" not in get.call_args.args[0]


@pytest.mark.parametrize(
    ("keyword", "category"),
    [
        ("kế toán", 1),
        ("Nhân viên kế toán", 1),
        ("nhân viên kinh doanh", 9),
        ("python developer", 13),
        ("thực tập marketing", 46),
        ("tài xế", 7),
        ("zzz", None),
    ],
)
def test_match_category(keyword, category):
    assert match_category(keyword) == category


def test_listing_urls():
    assert listing_url(None, None) == LATEST_URL
    assert listing_url(1, None) == f"{BASE_URL}/tim-viec-lam-ke-toan"
    assert listing_url(13, None) == f"{BASE_URL}/viec-lam-it-phan-mem-c13v0"
    assert listing_url(None, 45, "ho-chi-minh") == f"{BASE_URL}/viec-lam-tai-ho-chi-minh-c0v45"
    assert listing_url(None, 1, "ha-noi") == f"{BASE_URL}/tim-viec-lam-tai-ha-noi.html"


def test_scrape_latest_paginates_and_stops_on_repeated_page():
    scraper, get = scraper_with("latest.html", "latest.html")

    jobs = scraper.scrape_latest(max_pages=3)

    assert len(jobs) == 2
    assert get.call_count == 2
    assert get.call_args_list[1].kwargs["params"] == {"page": 2}


def test_live_mode_reads_a_single_page():
    scraper, get = scraper_with("latest.html", "latest.html")
    scraper.use_live_budget(None, 6.0)

    scraper.scrape_latest(max_pages=3)

    assert get.call_count == 1


def test_page_without_cards_flags_layout_change():
    scraper = TimViec365Scraper()
    response = Mock()
    response.text = "<html><body><h2><a href='/x-p1.html'>Old layout</a></h2></body></html>"
    scraper._get = Mock(return_value=response)

    assert scraper.scrape("kế toán", max_pages=1) == []
    assert scraper.last_error == "layout_changed"
