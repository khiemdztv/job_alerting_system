import json
import re
from pathlib import Path
from unittest.mock import Mock

from src.scrapers.vieclam24h_scraper import ViecLam24hScraper, province_id

FIXTURES = Path(__file__).parent / "fixtures" / "vieclam24h"


def response(text):
    result = Mock()
    result.text = text
    return result


def fixture():
    return (FIXTURES / "search.html").read_text(encoding="utf-8")


def without_next_data(html):
    return re.sub(r'<script id="__NEXT_DATA__".*?</script>', "", html, flags=re.S)


def test_reads_embedded_json_with_card_urls(monkeypatch):
    scraper = ViecLam24hScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response(fixture())))

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert len(jobs) == 3
    first, multi, negotiable = jobs
    assert first.title == "Kế Toán Tổng Hợp"
    assert first.company == "Công Ty TNHH Trung Tâm Da Liễu Thẩm Mỹ Quốc Tế Wedo"
    assert first.salary_raw == "12 - 20 triệu"
    assert first.location == "TP.HCM"
    assert first.job_type_raw == "Toàn thời gian cố định"
    assert first.posted_at_raw == "2026-10-30T19:57:24+07:00"  # approved_at, not scrape time
    assert first.valid_through_raw.startswith("2026-")
    assert first.source_url == (
        "https://vieclam24h.vn/ke-toan/ke-toan-tong-hop-c17p210000187id200979537.html"
    )  # open_from / search_id tracking removed
    assert first.description.startswith("Kỹ năng:")
    assert multi.location == "Tây Ninh, TP.HCM"
    assert negotiable.salary_raw == "Thoả thuận"


def test_batch_params_sort_newest_and_filter_province(monkeypatch):
    scraper = ViecLam24hScraper()
    get = Mock(return_value=response(fixture()))
    monkeypatch.setattr(scraper, "_get", get)

    scraper.scrape("kế toán", max_pages=3, location="Bình Dương")

    get.assert_called_once()  # fixture says total_pages=1
    params = get.call_args.kwargs["params"]
    assert params == {
        "sort_q": "actived_at_by_box,desc",
        "page": 1,
        "q": "kế toán",
        "province_ids[]": 210000187,  # Bình Dương merged into TP.HCM in 2025
    }


def test_live_mode_uses_default_ranking(monkeypatch):
    scraper = ViecLam24hScraper()
    scraper.use_live_budget(None, 3.0)
    get = Mock(return_value=response(fixture()))
    monkeypatch.setattr(scraper, "_get", get)

    scraper.scrape("kế toán", max_pages=1)

    assert "sort_q" not in get.call_args.kwargs["params"]


def test_scrape_latest_has_no_keyword(monkeypatch):
    scraper = ViecLam24hScraper()
    get = Mock(return_value=response(fixture()))
    monkeypatch.setattr(scraper, "_get", get)

    jobs = scraper.scrape_latest(max_pages=2)

    assert len(jobs) == 3
    assert get.call_args.kwargs["params"] == {"sort_q": "actived_at_by_box,desc", "page": 1}


def test_follows_total_pages(monkeypatch):
    html = fixture().replace('"total_pages": 1', '"total_pages": 2')
    scraper = ViecLam24hScraper()
    get = Mock(return_value=response(html))
    monkeypatch.setattr(scraper, "_get", get)

    scraper.scrape("kế toán", max_pages=5)

    assert [call.kwargs["params"]["page"] for call in get.call_args_list] == [1, 2]


def test_html_cards_are_the_fallback(monkeypatch):
    scraper = ViecLam24hScraper()
    monkeypatch.setattr(
        scraper, "_get", Mock(return_value=response(without_next_data(fixture())))
    )

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert len(jobs) == 3
    assert jobs[0].title == "Kế Toán Tổng Hợp"
    assert jobs[0].location == "TP.HCM"
    assert jobs[2].salary_raw == "Thoả thuận"
    assert "?" not in jobs[0].source_url
    assert scraper.last_error == ""


def test_genuine_zero_results_is_not_an_error(monkeypatch):
    body = {"props": {"initialProps": {"pageProps": {"jobsResponse": {"items": []}}}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(body)}</script>'
    scraper = ViecLam24hScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response(html)))

    assert scraper.scrape("xqzvwkplm", max_pages=1) == []
    assert scraper.last_error == ""


def test_page_without_json_or_cards_is_layout_change(monkeypatch):
    scraper = ViecLam24hScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response("<html><body/></html>")))

    assert scraper.scrape("kế toán", max_pages=1) == []
    assert scraper.last_error == "layout_changed"


def test_province_aliases():
    assert province_id("HCM") == 210000187
    assert province_id("TP. Hồ Chí Minh") == 210000187
    assert province_id("Hà Nội") == 210000160
    assert province_id("Quảng Nam") == 210000180  # merged into Đà Nẵng
    assert province_id("Atlantis") is None
