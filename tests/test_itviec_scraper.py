from pathlib import Path
from unittest.mock import Mock

import requests

from src.scrapers.base_scraper import LAYOUT_CHANGED
from src.scrapers.itviec_scraper import ITviecScraper

HTML = (Path(__file__).parent / "fixtures" / "itviec" / "search_page.html").read_text(
    encoding="utf-8"
)


def page(text):
    result = Mock()
    result.text = text
    return result


def test_parses_cards_from_real_page(monkeypatch):
    scraper = ITviecScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=page(HTML)))

    jobs = scraper.scrape("python", max_pages=1)

    assert len(jobs) == 4
    first = jobs[0]
    assert first.title.startswith("Agentic Engineer")
    assert first.company == "MB Bank"
    assert first.location == "Ha Noi"
    assert first.job_type_raw == "At office"
    assert first.posted_at_raw == "Posted 2 hours ago"
    assert first.salary_raw == ""  # "Sign in to view salary" is not a salary
    assert "Python" in first.description
    assert first.source_url == (
        "https://itviec.com/it-jobs/agentic-engineer-python-go-c-c-backend-ai-agents-mb-bank-5127"
    )
    assert {j.job_type_raw for j in jobs} <= {"At office", "Hybrid", "Remote"}
    assert all(j.location and j.company for j in jobs)


def test_city_goes_into_path_and_keyword_into_query(monkeypatch):
    scraper = ITviecScraper()
    get = Mock(return_value=page(HTML))
    monkeypatch.setattr(scraper, "_get", get)

    scraper.scrape("kế toán", max_pages=1, location="Hồ Chí Minh")

    assert get.call_args.args[0] == "https://itviec.com/it-jobs/ho-chi-minh-hcm"
    assert get.call_args.kwargs["params"] == {"page": 1, "query": "kế toán"}


def test_scrape_latest_uses_plain_listing(monkeypatch):
    scraper = ITviecScraper()
    get = Mock(return_value=page(HTML))
    monkeypatch.setattr(scraper, "_get", get)
    assert len(scraper.scrape_latest(max_pages=1)) == 4
    assert get.call_args.args[0] == "https://itviec.com/it-jobs"
    assert "query" not in get.call_args.kwargs["params"]


def test_visible_salary_is_kept():
    from bs4 import BeautifulSoup

    card = BeautifulSoup(
        '<div class="job-card" data-search--job-selection-job-slug-value="dev-1">'
        "<h3>Dev</h3><div class='salary'>1,000 - 2,000 USD</div></div>",
        "html.parser",
    ).div
    assert ITviecScraper().parse_job(card).salary_raw == "1,000 - 2,000 USD"


def test_200_without_cards_marks_layout_changed(monkeypatch):
    scraper = ITviecScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=page("<html><body>hi</body></html>")))
    assert scraper.scrape("python", max_pages=1) == []
    assert scraper.last_error == LAYOUT_CHANGED


def test_404_means_no_results_not_error(monkeypatch):
    scraper = ITviecScraper()
    response = Mock(status_code=404)
    monkeypatch.setattr(scraper, "_get", Mock(side_effect=requests.HTTPError(response=response)))
    assert scraper.scrape("zzzqqxx", max_pages=1) == []
    assert scraper.last_error == ""
