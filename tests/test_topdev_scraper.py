"""TopDev scraper against trimmed real api.topdev.vn responses (30 Oct 2026)."""

import json
from pathlib import Path
from unittest.mock import Mock

from src.common.models import JobSource
from src.etl.transformer import Transformer
from src.scrapers.topdev_scraper import API_URL, TopDevScraper

FIXTURES = Path(__file__).parent / "fixtures" / "topdev"


def payload(name: str) -> Mock:
    response = Mock()
    response.json.return_value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return response


def scraper_with(*names: str, **kwargs) -> tuple[TopDevScraper, Mock]:
    scraper = TopDevScraper(**kwargs)
    get = Mock(side_effect=[payload(name) for name in names])
    scraper._get = get
    return scraper, get


def test_parses_api_jobs():
    scraper, _ = scraper_with("search_intern.json")

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert len(jobs) == 4
    first, second = jobs[0], jobs[1]
    assert first.title == "Thực Tập Sinh Kế Toán"
    assert first.company == "TẬP ĐOÀN NOVAON"
    assert first.location == "Hà Nội"
    assert first.source is JobSource.TOPDEV
    assert first.source_url == (
        "https://topdev.vn/viec-lam/thuc-tap-sinh-ke-toan-tap-doan-novaon-2141735"
    )
    assert first.posted_at_raw == "2026-10-28T00:00:00+07:00"
    assert first.job_type_raw == "Trực tiếp, Thực tập"
    # Masked salaries ("1****** - 2****** VND") are dropped, negotiable ones kept.
    assert first.salary_raw == ""
    assert second.salary_raw == "Thương lượng"
    assert second.location == "Hồ Chí Minh, Bình Dương"
    assert scraper.last_error == ""


def test_query_uses_api_with_keyword_and_region():
    scraper, get = scraper_with("search_intern.json")

    scraper.scrape("kế toán", max_pages=1, location="TP.HCM")

    assert get.call_args.args[0] == API_URL
    params = get.call_args.kwargs["params"]
    assert params["keyword"] == "kế toán"
    assert params["region_ids"] == "79"
    assert params["page"] == 1
    assert params["page_size"] == 50
    assert "job_levels_ids" not in params


def test_internship_mode_filters_level_and_labels_jobs():
    scraper, get = scraper_with("search_intern.json", internship_only=True)

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert get.call_args.kwargs["params"]["job_levels_ids"] == "1616"
    assert jobs[0].job_type_raw.startswith("internship")
    assert Transformer().transform(jobs[0]).job_type.value == "internship"


def test_scrape_latest_has_no_keyword_and_stops_at_requested_pages():
    scraper, get = scraper_with("latest.json", "latest.json")

    jobs = scraper.scrape_latest(max_pages=2)

    assert len(jobs) == 6
    assert get.call_count == 2
    assert "keyword" not in get.call_args.kwargs["params"]


def test_stops_at_last_page():
    scraper, get = scraper_with("search_intern.json", "search_intern.json")

    scraper.scrape("kế toán", max_pages=5)

    assert get.call_count == 1


def test_unexpected_payload_flags_layout_change():
    scraper = TopDevScraper()
    response = Mock()
    response.json.return_value = {"error": "gone"}
    scraper._get = Mock(return_value=response)

    assert scraper.scrape("java", max_pages=1) == []
    assert scraper.last_error == "layout_changed"


def test_html_instead_of_json_flags_layout_change():
    scraper = TopDevScraper()
    response = Mock()
    response.json.side_effect = ValueError("not json")
    scraper._get = Mock(return_value=response)

    assert scraper.scrape("java", max_pages=1) == []
    assert scraper.last_error == "layout_changed"
