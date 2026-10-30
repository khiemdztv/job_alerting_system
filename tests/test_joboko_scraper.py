from pathlib import Path
from unittest.mock import Mock

import requests

from src.common.models import JobSource
from src.scrapers.joboko_scraper import JobokoScraper

FIX = Path(__file__).parent / "fixtures" / "joboko"
RICH = (FIX / "tim_viec_lam_nhan_vien_ke_toan.html").read_text(encoding="utf-8")
LATEST = (FIX / "viec_lam_moi.html").read_text(encoding="utf-8")


def response(text):
    result = Mock()
    result.text = text
    return result


def not_found():
    error = requests.HTTPError("404")
    error.response = Mock(status_code=404)
    return error


def test_parses_rich_cards(monkeypatch):
    scraper = JobokoScraper()
    get = Mock(return_value=response(RICH))
    monkeypatch.setattr(scraper, "_get", get)

    jobs = scraper.scrape("nhân viên kế toán", max_pages=1)

    assert len(jobs) == 6
    first = jobs[0]
    assert first.source == JobSource.JOBOKO
    assert first.title.startswith("[Hà Nội] Chuyên Viên Kế Toán Tổng Hợp")
    assert first.company.startswith("Công ty Cổ phần Đầu tư và Thi công Hạ tầng")
    assert first.location == "Hà Nội"
    assert first.salary_raw == "20.000.000 - 30.000.000 VND"
    assert "Hạn nộp hồ sơ: 26/11/2026" in first.description
    assert first.source_url.endswith("-xvi6755162")
    assert get.call_args.args[0] == "https://vn.joboko.com/tim-viec-lam-nhan-vien-ke-toan"


def test_404_slug_falls_back_to_nhan_vien_prefix(monkeypatch):
    scraper = JobokoScraper()
    get = Mock(side_effect=[not_found(), response(RICH)])
    monkeypatch.setattr(scraper, "_get", get)

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert len(jobs) == 6
    assert [c.args[0] for c in get.call_args_list] == [
        "https://vn.joboko.com/tim-viec-lam-ke-toan",
        "https://vn.joboko.com/tim-viec-lam-nhan-vien-ke-toan",
    ]


def test_unknown_keyword_reports_no_listing(monkeypatch):
    scraper = JobokoScraper()
    monkeypatch.setattr(scraper, "_get", Mock(side_effect=not_found()))

    assert scraper.scrape("zzzz", max_pages=1) == []
    assert scraper.last_error == "no_listing_for_keyword"


def test_location_variant_and_pagination(monkeypatch):
    scraper = JobokoScraper()
    get = Mock(return_value=response(RICH))
    monkeypatch.setattr(scraper, "_get", get)

    scraper.scrape("nhân viên kế toán", max_pages=2, location="Hà Nội")

    assert get.call_args_list[0].args[0].endswith("/tim-viec-lam-nhan-vien-ke-toan-tai-ha-noi")
    assert get.call_args_list[1].kwargs["params"] == {"p": 2}


def test_scrape_latest_reads_compact_cards(monkeypatch):
    scraper = JobokoScraper()
    get = Mock(return_value=response(LATEST))
    monkeypatch.setattr(scraper, "_get", get)

    jobs = scraper.scrape_latest(max_pages=1)

    assert len(jobs) == 8
    assert jobs[0].title == "Thực Tập Sinh Kế Toán"
    assert jobs[0].company == "Công ty cổ phần TMSX và XNK Nông Nghiệp Xanh"
    assert jobs[0].salary_raw == "3 - 5 triệu VNĐ"
    assert jobs[0].location == "Hà Nội"
    assert get.call_args.args[0] == "https://vn.joboko.com/viec-lam-moi"


def test_live_mode_single_request_and_layout_changed(monkeypatch):
    scraper = JobokoScraper()
    scraper.live_mode = True
    get = Mock(return_value=response("<html><body>blocked page</body></html>"))
    monkeypatch.setattr(scraper, "_get", get)

    assert scraper.scrape("nhân viên kế toán", max_pages=4) == []
    assert get.call_count == 1
    assert scraper.last_error == "layout_changed"
