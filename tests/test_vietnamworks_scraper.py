import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from src.scrapers.vietnamworks_scraper import VietnamWorksScraper, city_id

FIXTURES = Path(__file__).parent / "fixtures" / "vietnamworks"


@pytest.fixture
def api_body():
    return json.loads((FIXTURES / "search_api.json").read_text(encoding="utf-8"))


def api_response(body):
    response = Mock()
    response.json = Mock(return_value=body)
    return response


def scraper_with(body):
    scraper = VietnamWorksScraper()
    scraper._post = Mock(return_value=api_response(body))
    scraper._get = Mock(side_effect=AssertionError("Next.js fallback must not run"))
    return scraper


def test_reads_real_api_fixture(api_body):
    jobs = scraper_with(api_body).scrape("kế toán", max_pages=1)

    assert len(jobs) == 3
    first, multi, paid = jobs
    assert first.title == "Senior Accountant"
    assert first.company == "Công Ty TNHH Ziegler Logistics Việt Nam"
    assert first.job_type_raw == "Toàn thời gian"
    assert first.posted_at_raw == "2026-10-30T19:17:57+07:00"
    assert first.valid_through_raw == "2026-11-29T23:59:59+07:00"
    assert first.source_url == (
        "https://www.vietnamworks.com/senior-accountant-1793362677005871394-2126085-jv"
    )
    assert "<" not in first.description  # HTML stripped
    assert "Kỹ năng:" in first.description
    assert multi.location == "Hà Nội, Hồ Chí Minh"
    assert paid.salary_raw == "17tr-25tr ₫/tháng"


def test_batch_search_sorts_newest_with_max_page_size_and_city_filter(api_body):
    scraper = scraper_with(api_body)

    scraper.scrape("kế toán", max_pages=3, location="TP. Hồ Chí Minh")

    scraper._post.assert_called_once()  # fixture says nbPages=1
    args, kwargs = scraper._post.call_args
    assert args[0] == "https://ms.vietnamworks.com/job-search/v1.0/search"
    payload = kwargs["json_data"]
    assert payload["query"] == "kế toán"
    assert payload["order"] == [{"field": "approvedOn", "value": "desc"}]
    assert payload["hitsPerPage"] == 100
    assert payload["page"] == 0
    assert payload["filter"] == [{"field": "workingLocations.cityId", "value": "29"}]
    assert kwargs["headers"]["X-Source"] == "Page-Container"


def test_live_mode_reads_one_relevance_page(api_body):
    api_body["meta"]["nbPages"] = 9
    scraper = scraper_with(api_body)
    scraper.use_live_budget(None, 3.0)

    jobs = scraper.scrape("kế toán", max_pages=5)

    assert len(jobs) == 3
    scraper._post.assert_called_once()
    payload = scraper._post.call_args.kwargs["json_data"]
    assert payload["order"] == []
    assert payload["hitsPerPage"] == 50


def test_scrape_latest_has_no_keyword_and_follows_pages(api_body):
    api_body["meta"]["nbPages"] = 2
    scraper = scraper_with(api_body)

    jobs = scraper.scrape_latest(max_pages=5)

    assert len(jobs) == 6
    pages = [call.kwargs["json_data"]["page"] for call in scraper._post.call_args_list]
    assert pages == [0, 1]
    assert all(call.kwargs["json_data"]["query"] == "" for call in scraper._post.call_args_list)


def test_empty_hits_with_positive_count_is_layout_change(api_body):
    body = copy.deepcopy(api_body)
    body["data"] = []
    body["meta"]["nbHits"] = 1275
    scraper = scraper_with(body)

    assert scraper.scrape("kế toán", max_pages=1) == []
    assert scraper.last_error == "layout_changed"


def test_genuine_zero_results_is_not_an_error(api_body):
    body = {"meta": {"code": 200, "nbHits": 0, "nbPages": 0}, "data": []}
    scraper = scraper_with(body)

    assert scraper.scrape("xqzvwkplm", max_pages=1) == []
    assert scraper.last_error == ""


def test_blocked_api_does_not_fall_back():
    scraper = VietnamWorksScraper()
    error = requests.HTTPError(response=Mock(status_code=403))
    scraper._post = Mock(side_effect=error)
    scraper._get = Mock()

    assert scraper.scrape("kế toán", max_pages=1) == []
    scraper._get.assert_not_called()


def test_city_aliases():
    assert city_id("HCM") == 29
    assert city_id("Hà Nội") == 24
    assert city_id("Thành phố Đà Nẵng") == 17
    assert city_id("Atlantis") is None
