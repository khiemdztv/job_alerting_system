import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import src.scrapers.ats_scraper as ats
from src.common.models import JobSource
from src.scrapers.ats_scraper import (
    AtsScraper,
    html_to_text,
    is_vietnam_location,
    keyword_matches,
    load_companies,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ats"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def response(body):
    result = Mock()
    result.json.return_value = body
    return result


@pytest.fixture(autouse=True)
def clear_cache():
    ats._CACHE.clear()
    yield
    ats._CACHE.clear()


def scraper_for(ats_name, slug, company, fixture, monkeypatch):
    scraper = AtsScraper([{"ats": ats_name, "slug": slug, "company": company}])
    get = Mock(return_value=response(load(fixture)))
    monkeypatch.setattr(scraper, "_get", get)
    return scraper, get


def test_greenhouse_maps_fields_and_filters_to_vietnam(monkeypatch):
    scraper, get = scraper_for("greenhouse", "flodesk", "Flodesk", "greenhouse", monkeypatch)
    jobs = scraper.scrape_latest()
    assert get.call_args.args[0] == (
        "https://boards-api.greenhouse.io/v1/boards/flodesk/jobs?content=true"
    )
    assert jobs and all("Da Nang" in j.location for j in jobs)
    job = jobs[0]
    assert job.source == JobSource.ATS
    assert job.company == "Flodesk"
    assert job.source_url.startswith("https://")
    assert job.posted_at_raw.startswith("2026-")
    assert "<" not in job.description and "&lt;" not in job.description
    assert 0 < len(job.description) <= 4000


def test_lever_filters_vietnam_and_reads_created_at(monkeypatch):
    scraper, get = scraper_for("lever", "shopback-2", "ShopBack", "lever", monkeypatch)
    jobs = scraper.scrape_latest()
    assert get.call_args.args[0] == "https://api.lever.co/v0/postings/shopback-2?mode=json"
    assert len(jobs) == 3  # the two non-Vietnam postings are dropped
    assert all(is_vietnam_location(j.location) for j in jobs)
    assert jobs[0].source_url.startswith("https://jobs.lever.co/shopback-2/")
    assert jobs[0].posted_at_raw.startswith("2026-")
    assert jobs[0].job_type_raw


def test_ashby_maps_remote_and_employment_type(monkeypatch):
    scraper, _ = scraper_for("ashby", "skymavis", "Sky Mavis", "ashby", monkeypatch)
    jobs = scraper.scrape_latest()
    assert jobs
    first = jobs[0]
    assert first.company == "Sky Mavis"
    assert first.source_url.startswith("https://jobs.ashbyhq.com/skymavis/")
    assert "remote" in first.job_type_raw
    assert first.posted_at_raw.startswith("2026-")
    assert first.description


def test_smartrecruiters_builds_public_url_and_requests_vietnam(monkeypatch):
    scraper, get = scraper_for(
        "smartrecruiters", "Gameloft", "Gameloft", "smartrecruiters", monkeypatch
    )
    jobs = scraper.scrape_latest()
    assert get.call_args.args[0] == (
        "https://api.smartrecruiters.com/v1/companies/Gameloft/postings"
    )
    assert get.call_args.kwargs["params"]["country"] == "vn"
    assert get.call_count == 1  # fewer than a full page: no further requests
    assert len(jobs) == 3
    assert jobs[0].source_url == "https://jobs.smartrecruiters.com/Gameloft/744000158655881"
    assert jobs[0].job_type_raw == "Full-time"
    assert "Hanoi" in jobs[0].location


def test_workable_keeps_only_vietnam_rows(monkeypatch):
    scraper, get = scraper_for("workable", "knorex", "KNOREX", "workable", monkeypatch)
    jobs = scraper.scrape_latest()
    assert get.call_args.args[0] == "https://apply.workable.com/api/v1/widget/accounts/knorex"
    assert {j.title for j in jobs} == {"Junior Software Engineer (RTB)", "MLOps Engineer"}
    assert jobs[0].source_url.startswith("https://apply.workable.com/j/")
    assert jobs[0].job_type_raw.startswith("Full-time")


def test_scrape_filters_by_keyword_locally(monkeypatch):
    scraper, _ = scraper_for("workable", "knorex", "KNOREX", "workable", monkeypatch)
    assert [j.title for j in scraper.scrape("mlops")] == ["MLOps Engineer"]
    assert scraper.scrape("kế toán") == []


def test_company_responses_are_cached_between_keywords(monkeypatch):
    scraper, get = scraper_for("workable", "knorex", "KNOREX", "workable", monkeypatch)
    scraper.scrape("engineer")
    scraper.scrape("mlops")
    scraper.scrape_latest()
    assert get.call_count == 1


def test_failing_company_does_not_hide_the_others(monkeypatch):
    scraper = AtsScraper([
        {"ats": "workable", "slug": "broken", "company": "Broken"},
        {"ats": "workable", "slug": "knorex", "company": "KNOREX"},
    ])
    good = response(load("workable"))

    def fake_get(url, **kwargs):
        if "broken" in url:
            raise requests.ConnectionError("down")
        return good

    monkeypatch.setattr(scraper, "_get", fake_get)
    assert len(scraper.scrape_latest()) == 2


def test_live_mode_reads_only_the_eight_best_ranked_companies(monkeypatch):
    companies = [
        {"ats": "workable", "slug": f"c{i}", "company": f"C{i}", "live_priority": i}
        for i in range(1, 13)
    ]
    companies.append({"ats": "workable", "slug": "slow", "company": "Slow", "live_priority": None})
    scraper = AtsScraper(companies)
    scraper.use_live_budget(time.monotonic() + 30, 6.0)
    get = Mock(return_value=response({"jobs": []}))
    monkeypatch.setattr(scraper, "_get", get)
    scraper.scrape("engineer")
    assert get.call_count == 8
    assert get.call_args_list[0].args[0].endswith("/c1")
    assert get.call_args_list[-1].args[0].endswith("/c8")


def test_budget_exhaustion_stops_quietly(monkeypatch):
    companies = [{"ats": "workable", "slug": s, "company": s} for s in ("a", "b", "c")]
    scraper = AtsScraper(companies)
    get = Mock(side_effect=TimeoutError("budget"))
    monkeypatch.setattr(scraper, "_get", get)
    assert scraper.scrape("x") == []
    assert scraper.last_error == "TimeoutError"
    assert get.call_count == 1


def test_location_filter_accepts_vietnam_and_apac_remote_only():
    assert is_vietnam_location("Ho Chi Minh City, Vietnam")
    assert is_vietnam_location("(On-site) Da Nang, Da Nang, Vietnam")
    assert is_vietnam_location("Hà Nội")
    assert is_vietnam_location("Remote - APAC")
    assert not is_vietnam_location("Remote")
    assert not is_vietnam_location("Singapore")
    assert not is_vietnam_location("")


def test_keyword_matching_uses_whole_words_without_accents():
    assert keyword_matches("kế toán", "Nhân viên KE TOAN tổng hợp")
    assert keyword_matches("data engineer", "Senior Engineer, Data Platform")
    assert not keyword_matches("it", "Fruit seller")
    assert keyword_matches("", "anything")


def test_html_to_text_handles_escaped_markup():
    assert html_to_text("&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;") == "Hello\nworld"
    assert len(html_to_text("<p>" + "x" * 9000 + "</p>")) == 4000


def test_companies_file_is_valid_and_has_live_candidates():
    companies = load_companies()
    assert 30 <= len(companies) <= 60
    assert all(c["ats"] in ats.API_URLS and c["slug"] and c["company"] for c in companies)
    keys = [(c["ats"], c["slug"]) for c in companies]
    assert len(keys) == len(set(keys))
    live = [c for c in companies if c.get("live_priority") is not None]
    assert len(live) >= ats.LIVE_MAX_COMPANIES
