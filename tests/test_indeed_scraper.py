import json
from pathlib import Path
from unittest.mock import Mock

from src.common.models import JobSource
from src.scrapers.indeed_scraper import ADMIN1_PROVINCES, IndeedScraper, _location, _salary

FIXTURE = Path(__file__).parent / "fixtures" / "indeed" / "search_ke_toan.json"


def response(body):
    result = Mock()
    result.json.return_value = body
    return result


def load():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_scrape_parses_real_response(monkeypatch):
    scraper = IndeedScraper()
    post = Mock(return_value=response(load()))
    monkeypatch.setattr(scraper, "_post", post)

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert len(jobs) == 4
    first = jobs[0]
    assert first.source == JobSource.INDEED
    assert first.source_url.startswith("https://vn.indeed.com/viewjob?jk=")
    assert first.title and first.company and first.description
    assert first.posted_at_raw.startswith("2026-")
    assert "<" not in first.description
    assert any(job.salary_raw for job in jobs)
    query = post.call_args.kwargs["json_data"]["query"]
    assert '"kế toán"' in query and '"Vietnam"' in query and "72h" in query


def test_salary_formats():
    vnd = {"baseSalary": {"unitOfWork": "MONTH", "range": {"min": 15000000.0, "max": 25000000.0}}, "currencyCode": "VND"}
    assert _salary(vnd) == "15 - 25 triệu/tháng"
    usd = {"baseSalary": {"unitOfWork": "YEAR", "range": {"min": 1000.0}}, "currencyCode": "USD"}
    assert _salary(usd) == "1,000 USD/năm"
    assert _salary({"baseSalary": None, "currencyCode": None}) == ""


def test_parse_job_handles_missing_fields():
    scraper = IndeedScraper()
    job = scraper.parse_job(
        {
            "key": "abc",
            "title": " Remote Dev ",
            "sourceEmployerName": "Acme",
            "dateOnIndeed": None,
            "location": {"countryCode": "VN", "city": None, "admin1Code": None},
            "attributes": [{"key": "DSQF7"}, {"key": "CF3CP"}],
            "employer": None,
            "description": None,
        }
    )
    assert job.title == "Remote Dev"
    assert job.location == "Việt Nam"
    assert job.job_type_raw == "full-time, remote"
    assert job.posted_at_raw == ""
    assert scraper.parse_job({"key": "", "title": "x"}) is None


def test_live_mode_makes_one_request_with_small_page(monkeypatch):
    body = load()
    body["data"]["jobSearch"]["pageInfo"]["nextCursor"] = "NEXT"
    scraper = IndeedScraper()
    scraper.use_live_budget(None, 5)
    post = Mock(return_value=response(body))
    monkeypatch.setattr(scraper, "_post", post)

    scraper.scrape("kế toán", max_pages=5)

    assert post.call_count == 1
    assert "limit: 40" in post.call_args.kwargs["json_data"]["query"]


def test_pagination_follows_cursor_and_dedups(monkeypatch):
    page1 = load()
    page1["data"]["jobSearch"]["pageInfo"]["nextCursor"] = "C2"
    page2 = load()
    page2["data"]["jobSearch"]["pageInfo"]["nextCursor"] = None
    scraper = IndeedScraper()
    post = Mock(side_effect=[response(page1), response(page2)])
    monkeypatch.setattr(scraper, "_post", post)

    jobs = scraper.scrape("kế toán", max_pages=3, location="Hanoi")

    assert post.call_count == 2
    assert 'cursor: "C2"' in post.call_args.kwargs["json_data"]["query"]
    assert '"Hanoi"' in post.call_args_list[0].kwargs["json_data"]["query"]
    assert len(jobs) == 4


def test_empty_search_sets_error(monkeypatch):
    scraper = IndeedScraper()
    monkeypatch.setattr(scraper, "_post", Mock(return_value=response({"data": None, "errors": []})))
    assert scraper.scrape("x") == []
    assert scraper.last_error == "NoJobSearch"


def test_location_is_always_a_name():
    assert _location({"city": "Hà Nội", "admin1Code": "HN"}) == "Hà Nội"
    assert _location({"city": "Thành phố Hồ Chí Minh", "admin1Code": "SG"}) == "Hồ Chí Minh"
    assert _location({"city": "Ðà Nẵng", "admin1Code": "DN"}) == "Đà Nẵng"
    assert _location({"city": None, "admin1Code": "39", "countryCode": "VN"}) == "Đồng Nai"
    assert _location({"admin1Code": "71", "countryCode": "VN"}) == "Điện Biên"
    assert _location({"admin1Code": "SG", "countryCode": "VN"}) == "Hồ Chí Minh"
    assert _location({"city": "HCMC"}) == "Hồ Chí Minh"
    assert _location({"city": "Sài Gòn"}) == "Hồ Chí Minh"
    assert _location({"admin1Code": "99", "countryCode": "VN"}) == "Việt Nam"
    assert len(ADMIN1_PROVINCES) >= 60


def test_fixture_locations_have_no_codes(monkeypatch):
    scraper = IndeedScraper()
    monkeypatch.setattr(scraper, "_post", Mock(return_value=response(load())))
    for job in scraper.scrape("kế toán"):
        assert not job.location.isdigit() and job.location not in {"SG", "HN"}
        assert "Ð" not in job.location
