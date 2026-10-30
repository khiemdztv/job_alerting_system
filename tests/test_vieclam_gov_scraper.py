import json
from pathlib import Path
from unittest.mock import Mock

import src.scrapers.vieclam_gov_scraper as module
from src.common.models import JobSource
from src.scrapers.vieclam_gov_scraper import VieclamGovScraper

PAYLOAD = json.loads(
    (Path(__file__).parent / "fixtures" / "vieclam_gov" / "search_ke_toan.json").read_text(
        encoding="utf-8"
    )
)


def api_response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


def make(monkeypatch, payload=PAYLOAD):
    monkeypatch.setattr(module, "_PROVINCES", {"thanh-pho-ha-noi": 1, "tinh-hung-yen": 14})
    scraper = VieclamGovScraper()
    post = Mock(return_value=api_response(payload))
    monkeypatch.setattr(scraper, "_post", post)
    return scraper, post


def test_parses_real_api_rows(monkeypatch):
    scraper, post = make(monkeypatch)

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert len(jobs) == 6
    first = jobs[0]
    assert first.source == JobSource.VIECLAM_GOV
    assert first.title == "Kế toán nội bộ"
    assert first.company == "CÔNG TY CỔ PHẦN NHÂN LỰC HOA ANH ĐÀO"
    assert first.location == "Thành phố Hồ Chí Minh"
    assert first.salary_raw == "10 đến 20 triệu"
    assert first.requirements == "1 – 2 năm"
    assert first.job_type_raw == "Toàn thời gian"
    assert first.posted_at_raw == "2026-10-30 08:25:28"
    assert "Hạn nộp: 31/12/2026" in first.description
    assert first.source_url == "https://vieclam.gov.vn/search/job-detail?id=563018"
    assert post.call_args.kwargs["json_data"] == {"tencv": "kế toán"}
    assert post.call_args.kwargs["params"] == {"page_num": 1, "page_size": 20}


def test_location_uses_province_id_filter(monkeypatch):
    scraper, post = make(monkeypatch)

    scraper.scrape("kế toán", max_pages=1, location="Hà Nội")

    assert post.call_args.kwargs["json_data"] == {"tencv": "kế toán", "noi_lam_viec": [1]}


def test_unknown_location_falls_back_to_post_filter(monkeypatch):
    scraper, _ = make(monkeypatch)

    jobs = scraper.scrape("kế toán", max_pages=1, location="Cà Mau")

    assert jobs == []


def test_stops_after_last_page_and_latest_has_empty_keyword(monkeypatch):
    payload = dict(PAYLOAD, pagination={"total_pages": 1})
    scraper, post = make(monkeypatch, payload)

    scraper.scrape_latest(max_pages=5)

    assert post.call_count == 1
    assert post.call_args.kwargs["json_data"] == {"tencv": ""}


def test_unexpected_payload_marks_layout_changed(monkeypatch):
    scraper, _ = make(monkeypatch, {"success": False, "message": "Input không hợp lệ"})

    assert scraper.scrape("kế toán", max_pages=1) == []
    assert scraper.last_error == "layout_changed"


def test_parse_job_requires_title_and_id():
    assert VieclamGovScraper().parse_job({"vitri_td": "", "id": 1}) is None
    assert VieclamGovScraper().parse_job({"vitri_td": "A"}) is None
