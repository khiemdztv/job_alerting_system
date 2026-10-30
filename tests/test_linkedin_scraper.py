from pathlib import Path
from unittest.mock import Mock

from src.common.models import JobSource
from src.scrapers.linkedin_scraper import LinkedInScraper

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin" / "guest_search.html"


def response(text):
    result = Mock()
    result.text = text
    return result


def html():
    return FIXTURE.read_text(encoding="utf-8")


def test_scrape_parses_real_cards(monkeypatch):
    scraper = LinkedInScraper()
    get = Mock(return_value=response(html()))
    monkeypatch.setattr(scraper, "_get", get)

    jobs = scraper.scrape("kế toán", max_pages=3)

    assert len(jobs) == 4
    first = jobs[0]
    assert first.source == JobSource.LINKEDIN
    assert first.title == "General Accountant"
    assert first.company == "AntAgri JSC"
    assert first.location == "Ho Chi Minh City, Vietnam"
    assert first.posted_at_raw == "2026-10-30"
    assert first.source_url == "https://www.linkedin.com/jobs/view/4484805495"
    # fewer than a full page of 10 cards means the end of results: no page 2
    assert get.call_count == 1
    params = get.call_args.kwargs["params"]
    assert params["keywords"] == "kế toán"
    assert params["location"] == "Vietnam"
    assert params["f_TPR"] == "r259200"
    assert params["start"] == 0


def test_full_page_fetches_next_page_and_dedups(monkeypatch):
    card = html().split("</li>")[0] + "</li>"
    full_page = "\n".join(card.replace("4484805495", str(1000 + i)) for i in range(10))
    scraper = LinkedInScraper()
    get = Mock(side_effect=[response(full_page), response(full_page)])
    monkeypatch.setattr(scraper, "_get", get)

    jobs = scraper.scrape("x", max_pages=2, location="Hanoi")

    assert get.call_count == 2
    assert get.call_args.kwargs["params"]["start"] == 10
    assert get.call_args.kwargs["params"]["location"] == "Hanoi"
    assert len(jobs) == 10


def test_live_mode_single_request(monkeypatch):
    card = html().split("</li>")[0] + "</li>"
    full_page = "\n".join(card.replace("4484805495", str(2000 + i)) for i in range(10))
    scraper = LinkedInScraper()
    scraper.use_live_budget(None, 5)
    get = Mock(return_value=response(full_page))
    monkeypatch.setattr(scraper, "_get", get)

    scraper.scrape("x", max_pages=5)

    assert get.call_count == 1


def test_empty_page_returns_nothing(monkeypatch):
    scraper = LinkedInScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response("<!DOCTYPE html>\n\n<!---->")))
    assert scraper.scrape("x") == []


def test_parse_job_rejects_cards_without_id_or_title():
    from bs4 import BeautifulSoup

    scraper = LinkedInScraper()
    card = BeautifulSoup('<div class="base-search-card"><h3 class="base-search-card__title">T</h3></div>', "html.parser").div
    assert scraper.parse_job(card) is None
