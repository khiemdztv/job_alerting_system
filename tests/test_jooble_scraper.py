import json
from pathlib import Path
from unittest.mock import Mock

from src.scrapers.base_scraper import LAYOUT_CHANGED
from src.scrapers.jooble_scraper import JoobleScraper

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "jooble" / "search.json").read_text(encoding="utf-8")
)


def api(payload):
    result = Mock()
    result.json.return_value = payload
    return result


def make(monkeypatch, payload, key="secret"):
    scraper = JoobleScraper()
    scraper.settings = Mock(jooble_api_key=key, scrape_max_pages=3)
    post = Mock(return_value=api(payload))
    monkeypatch.setattr(scraper, "_post", post)
    return scraper, post


def test_parses_and_cleans_html(monkeypatch):
    scraper, post = make(monkeypatch, FIXTURE)
    jobs = scraper.scrape("data engineer", max_pages=2, location="Hà Nội")

    assert len(jobs) == 1  # the blank-title entry is dropped
    job = jobs[0]
    assert job.title == "Senior Data Engineer"
    assert job.description == "... build data pipelines on AWS & Airflow ..."
    assert job.salary_raw == "$2,000 - $3,500"
    assert job.job_type_raw == "Full-time"
    assert job.posted_at_raw == "2026-10-29T08:15:30.1230000"
    assert post.call_count == 1  # totalCount (2) fits on the first page
    assert post.call_args.args[0] == "https://jooble.org/api/secret"
    assert post.call_args.kwargs["json_data"]["location"] == "Hà Nội"


def test_default_location_is_vietnam(monkeypatch):
    scraper, post = make(monkeypatch, FIXTURE)
    scraper.scrape("x", max_pages=1)
    assert post.call_args.kwargs["json_data"]["location"] == "Vietnam"


def test_no_key_is_reported(monkeypatch):
    scraper, post = make(monkeypatch, FIXTURE, key=None)
    assert scraper.scrape("x") == []
    assert scraper.last_error == "no_api_key"
    post.assert_not_called()


def test_payload_without_jobs_marks_layout_changed(monkeypatch):
    scraper, _ = make(monkeypatch, {"message": "oops"})
    assert scraper.scrape("x", max_pages=1) == []
    assert scraper.last_error == LAYOUT_CHANGED
