"""CareerLink scraper against trimmed real pages captured on 30 Oct 2026."""

from pathlib import Path
from unittest.mock import Mock

from src.common.models import JobSource
from src.etl.transformer import Transformer
from src.scrapers.careerlink_scraper import CareerLinkScraper, city_slug

FIXTURES = Path(__file__).parent / "fixtures" / "careerlink"


def page(name: str) -> Mock:
    response = Mock()
    response.text = (FIXTURES / name).read_text(encoding="utf-8")
    return response


def scraper_with(*pages: str) -> tuple[CareerLinkScraper, Mock]:
    scraper = CareerLinkScraper()
    get = Mock(side_effect=[page(name) for name in pages])
    scraper._get = get
    return scraper, get


def test_parses_current_next_js_cards():
    scraper, _ = scraper_with("search_ha_noi.html")

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert len(jobs) == 3
    first = jobs[0]
    assert first.title == "[Hà Nội] Thực Tập Sinh Kế Toán"
    assert first.company == "Công Ty Cổ Phần Bán Lẻ Kỹ Thuật Số FPT"
    assert first.location == "Hà Nội"
    assert first.salary_raw == "2 triệu - 2.5 triệu"
    assert first.job_type_raw == "Sinh viên / Thực tập sinh"
    assert first.posted_at_raw == "2026-10-29T17:00:00.000Z"
    assert first.source is JobSource.CAREERLINK
    assert first.source_url == (
        "https://www.careerlink.vn/tim-viec-lam/ha-noi-thuc-tap-sinh-ke-toan/3664601"
    )
    # Several cities are kept, comma-separated.
    assert jobs[2].location == "Hồ Chí Minh, Hà Nội"
    assert scraper.last_error == ""


def test_query_sorts_by_posting_date_and_filters_city_by_path():
    scraper, get = scraper_with("search_ha_noi.html")

    scraper.scrape("kế toán", max_pages=1, location="TP. Hà Nội")

    url = get.call_args.args[0]
    params = get.call_args.kwargs["params"]
    assert url == "https://www.careerlink.vn/tim-viec-lam-tai/ha-noi/HN"
    assert params == {"keyword": "kế toán", "order": "posted_date", "sort": "desc"}


def test_unknown_location_falls_back_to_national_search():
    scraper, get = scraper_with("search_ha_noi.html")

    scraper.scrape("kế toán", max_pages=1, location="Atlantis")

    assert get.call_args.args[0] == CareerLinkScraper.SEARCH_URL


def test_follows_pagination_until_max_pages():
    scraper, get = scraper_with("search_ha_noi.html", "latest.html")

    jobs = scraper.scrape("kế toán", max_pages=2)

    assert len(jobs) == 5
    assert get.call_args_list[1].kwargs["params"]["page"] == 2


def test_live_mode_reads_a_single_page():
    scraper, get = scraper_with("search_ha_noi.html", "latest.html")
    scraper.use_live_budget(None, 4.0)

    scraper.scrape("kế toán", max_pages=3)

    assert get.call_count == 1


def test_scrape_latest_has_no_keyword():
    scraper, get = scraper_with("latest.html")

    jobs = scraper.scrape_latest(max_pages=1)

    assert len(jobs) == 2
    assert "keyword" not in get.call_args.kwargs["params"]
    assert get.call_args.kwargs["params"]["order"] == "posted_date"


def test_genuine_no_result_page_is_not_a_layout_change():
    scraper, _ = scraper_with("no_results.html")

    assert scraper.scrape("zzqxwvkjh", max_pages=1) == []
    assert scraper.last_error == ""


def test_page_without_cards_flags_layout_change():
    scraper = CareerLinkScraper()
    response = Mock()
    response.text = "<html><body><ul><li class='job-item'>old layout</li></ul></body></html>"
    scraper._get = Mock(return_value=response)

    assert scraper.scrape("kế toán", max_pages=1) == []
    assert scraper.last_error == "layout_changed"


def test_iso_dates_and_salaries_survive_the_transformer():
    scraper, _ = scraper_with("search_ha_noi.html")

    job = Transformer().transform(scraper.scrape("kế toán", max_pages=1)[1])

    assert job.posted_at.isoformat() == "2026-10-29T17:00:00+00:00"
    assert job.salary_min == 10_000_000


def test_city_slug_normalises_common_spellings():
    assert city_slug("TP. Hồ Chí Minh") == "ho-chi-minh"
    assert city_slug("HCM") == "ho-chi-minh"
    assert city_slug("Hà Nội") == "ha-noi"
