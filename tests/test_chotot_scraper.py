import json
from pathlib import Path
from unittest.mock import Mock

from src.scrapers.base_scraper import LAYOUT_CHANGED
from src.scrapers.chotot_scraper import ChototScraper

FIXTURE = Path(__file__).parent / "fixtures" / "chotot" / "ad_listing.json"


def api(payload):
    result = Mock()
    result.json.return_value = payload
    return result


def load():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parses_real_ads():
    scraper = ChototScraper()
    jobs = [scraper.parse_job(ad) for ad in load()["ads"]]
    assert all(jobs)
    first = jobs[0]
    assert first.title == "KẾ TOÁN"
    assert first.company == "CÔNG TY TNHH HẰNG VIỄN HƯNG"
    assert first.location == "Quận 6, Tp Hồ Chí Minh"
    assert first.salary_raw == "18 - 20 triệu"
    assert first.job_type_raw == "Toàn thời gian"
    assert first.posted_at_raw == "1792486856000"  # original post date, not the bump time
    assert first.source_url == "https://www.chotot.com/135363378.htm"


def test_query_uses_max_page_size_region_and_offset(monkeypatch):
    scraper = ChototScraper()
    get = Mock(return_value=api({"ads": load()["ads"], "total": 500}))
    monkeypatch.setattr(scraper, "_get", get)

    jobs = scraper.scrape("kế toán", max_pages=2, location="Hà Nội")

    assert len(jobs) == 2 * len(load()["ads"])
    first, second = (c.kwargs["params"] for c in get.call_args_list)
    assert first["q"] == "kế toán" and first["limit"] == 50 and first["o"] == 0
    assert first["region_v2"] == 12000 and first["cg"] == 13000
    assert second["o"] == 50


def test_scrape_latest_has_no_keyword(monkeypatch):
    scraper = ChototScraper()
    get = Mock(return_value=api({"ads": load()["ads"], "total": 3}))
    monkeypatch.setattr(scraper, "_get", get)

    assert len(scraper.scrape_latest(max_pages=3)) == 3
    assert "q" not in get.call_args.kwargs["params"]
    assert scraper.supports_latest


def test_unknown_location_is_ignored(monkeypatch):
    scraper = ChototScraper()
    get = Mock(return_value=api({"ads": [], "total": 0, "x": 1}))
    monkeypatch.setattr(scraper, "_get", get)
    scraper.scrape("x", max_pages=1, location="Cần Thơ")
    assert "region_v2" not in get.call_args.kwargs["params"]
    assert ChototScraper.region_code("Hồ Chí Minh") == 13000


def test_part_time_and_price_string_fallback():
    job = ChototScraper().parse_job(
        {"list_id": 1, "subject": "Phục vụ", "contract_type": 2, "price_string": "Thỏa thuận"}
    )
    assert job.job_type_raw == "Bán thời gian"
    assert job.salary_raw == ""


def test_unexpected_payload_marks_layout_changed(monkeypatch):
    scraper = ChototScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=api({"error": "x"})))
    assert scraper.scrape("kế toán", max_pages=1) == []
    assert scraper.last_error == LAYOUT_CHANGED


def test_empty_result_is_not_an_error(monkeypatch):
    scraper = ChototScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=api({"ads": [], "total": 0})))
    assert scraper.scrape("zzz", max_pages=1) == []
    assert scraper.last_error == ""
