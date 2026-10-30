import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import src.scrapers.remote_api_scraper as remote
from src.common.models import JobSource
from src.scrapers.remote_api_scraper import REMOTE_LOCATION, RemoteApiScraper

FIXTURES = Path(__file__).parent / "fixtures" / "remote_api"
HOSTS = {
    "himalayas.app": "himalayas",
    "remotive.com": "remotive",
    "arbeitnow.com": "arbeitnow",
    "jobicy.com": "jobicy",
}


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def response(body):
    result = Mock()
    result.json.return_value = body
    return result


@pytest.fixture(autouse=True)
def clear_cache():
    remote._CACHE.clear()
    yield
    remote._CACHE.clear()


def serve(url, params=None, **kwargs):
    for host, name in HOSTS.items():
        if host in url:
            return response(load(name))
    raise AssertionError(url)


def make_scraper(monkeypatch):
    scraper = RemoteApiScraper()
    get = Mock(side_effect=serve)
    monkeypatch.setattr(scraper, "_get", get)
    return scraper, get


def by_api(jobs, api):
    return [j for j in jobs if j.company.endswith(f"(via {api})")]


def calls_to(get, host):
    return sum(host in c.args[0] for c in get.call_args_list)


def test_every_posting_is_tagged_remote_and_attributed(monkeypatch):
    scraper, _ = make_scraper(monkeypatch)
    jobs = scraper.scrape_latest()
    assert {j.source for j in jobs} == {JobSource.REMOTE_API}
    for api in ("Himalayas", "Remotive", "Arbeitnow", "Jobicy"):
        assert by_api(jobs, api), api
    for job in jobs:
        assert job.location == REMOTE_LOCATION
        assert job.job_type_raw.startswith("remote")
        assert job.source_url.startswith("https://")
        assert "(via " in job.description
        assert "<" not in job.description
        assert len(job.description) <= 4000


def test_original_urls_are_kept(monkeypatch):
    scraper, _ = make_scraper(monkeypatch)
    jobs = scraper.scrape_latest()
    assert all("himalayas.app" in j.source_url for j in by_api(jobs, "Himalayas"))
    assert all("remotive.com" in j.source_url for j in by_api(jobs, "Remotive"))
    assert all("arbeitnow.com" in j.source_url for j in by_api(jobs, "Arbeitnow"))
    assert all("jobicy.com" in j.source_url for j in by_api(jobs, "Jobicy"))


def test_himalayas_is_searched_for_vietnam(monkeypatch):
    scraper, get = make_scraper(monkeypatch)
    scraper.scrape("developer")
    himalayas = [c for c in get.call_args_list if "himalayas" in c.args[0]]
    assert himalayas[0].kwargs["params"]["country"] == "VN"
    assert himalayas[0].kwargs["params"]["q"] == "developer"


def test_remotive_and_jobicy_drop_postings_closed_to_vietnam(monkeypatch):
    scraper, _ = make_scraper(monkeypatch)
    jobs = scraper.scrape_latest()
    assert len(by_api(jobs, "Remotive")) == 3  # the Europe/USA-only ones are dropped
    assert len(by_api(jobs, "Jobicy")) == 2  # the Latin-America-only one is dropped


def test_arbeitnow_keeps_only_remote_postings(monkeypatch):
    scraper, _ = make_scraper(monkeypatch)
    assert len(by_api(scraper.scrape_latest(), "Arbeitnow")) == 2


def test_keyword_filters_locally(monkeypatch):
    scraper, _ = make_scraper(monkeypatch)
    jobs = scraper.scrape("vectorworks")
    assert jobs
    assert all("vectorworks" in (j.title + j.description).lower() for j in jobs)
    assert scraper.scrape("kế toán xyz") == []


def test_call_capped_apis_are_called_once_per_process(monkeypatch):
    scraper, get = make_scraper(monkeypatch)
    scraper.scrape("developer")
    scraper.scrape("engineer")
    scraper.scrape_latest()
    assert calls_to(get, "remotive.com") == 1
    assert calls_to(get, "jobicy.com") == 1
    assert calls_to(get, "arbeitnow.com") == 2  # two pages, fetched once


def test_live_mode_skips_every_api(monkeypatch):
    scraper, get = make_scraper(monkeypatch)
    scraper.use_live_budget(None, 5.0)
    assert scraper.scrape("developer") == []
    assert scraper.scrape_latest() == []
    get.assert_not_called()


def test_one_api_failing_keeps_the_others(monkeypatch):
    scraper, get = make_scraper(monkeypatch)

    def flaky(url, params=None, **kwargs):
        if "remotive" in url:
            raise ConnectionError("down")
        return serve(url, params)

    get.side_effect = flaky
    jobs = scraper.scrape_latest()
    assert not by_api(jobs, "Remotive")
    assert by_api(jobs, "Himalayas") and by_api(jobs, "Jobicy")
