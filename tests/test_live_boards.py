from datetime import datetime, timezone
from unittest.mock import Mock

from src.common.models import JobSource, RawJob
from src.web_search.live_boards import search_live_job_boards


def raw(title, company, location, source, url):
    return RawJob(
        title=title,
        company=company,
        location=location,
        source=source,
        source_url=url,
        scraped_at=datetime.now(timezone.utc),
    )


def test_live_boards_find_active_related_ai_internships(monkeypatch):
    vieclam = Mock()
    vieclam.source = JobSource.VIECLAM24H
    vieclam.scrape.return_value = [
        raw(
            "Intern AI - ERP - Software Developer",
            "T4 Tek",
            "TP.HCM",
            JobSource.VIECLAM24H,
            "https://vieclam24h.vn/job/1?search_id=tracking&open_from=list",
        ),
        raw(
            "AI Engineer Intern",
            "Wrong City",
            "Hà Nội",
            JobSource.VIECLAM24H,
            "https://vieclam24h.vn/job/2",
        ),
    ]
    careerviet = Mock()
    careerviet.source = JobSource.CAREERVIET
    careerviet.scrape.return_value = [
        raw(
            "Project cum AI Intern",
            "CareerViet",
            "Hồ Chí Minh",
            JobSource.CAREERVIET,
            "https://careerviet.vn/vi/tim-viec-lam/project-ai.1.html",
        )
    ]
    monkeypatch.setattr("src.web_search.live_boards.ViecLam24hScraper", Mock(return_value=vieclam))
    monkeypatch.setattr(
        "src.web_search.live_boards.CareerVietScraper", Mock(return_value=careerviet)
    )
    for name in ("JobsGoScraper", "TopDevScraper", "VietnamWorksScraper"):
        scraper = Mock()
        scraper.source = JobSource.JOBSGO
        scraper.scrape.return_value = []
        monkeypatch.setattr(f"src.web_search.live_boards.{name}", Mock(return_value=scraper))

    jobs = search_live_job_boards("ai engineer intern", location="HCM", limit=10)

    assert [job["title"] for job in jobs] == [
        "Project cum AI Intern",
        "Intern AI - ERP - Software Developer",
    ]
    assert jobs[1]["source_url"] == "https://vieclam24h.vn/job/1"
    assert all(job["is_live_listing"] for job in jobs)
