import json
from unittest.mock import Mock

from src.scrapers.jobsgo_scraper import JobsGoScraper
from src.scrapers.topdev_scraper import TopDevScraper
from src.scrapers.vietnamworks_scraper import VietnamWorksScraper


def response(text):
    result = Mock()
    result.text = text
    return result


def test_jobsgo_reads_current_job_cards(monkeypatch):
    html = """
    <div class="job-card">
      <h3 class="job-title">
        <a href="/viec-lam/ai-intern-123.html?ref_component=list">AI Intern</a>
      </h3>
      <a class="company-title">IMT Solutions</a>
      <span>Từ 1 triệu VNĐ</span><span>Hồ Chí Minh</span><span>1 ngày trước</span>
    </div>
    """
    scraper = JobsGoScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response(html)))

    jobs = scraper.scrape("ai intern", max_pages=1)

    assert len(jobs) == 1
    assert jobs[0].title == "AI Intern"
    assert jobs[0].company == "IMT Solutions"
    assert jobs[0].location == "Hồ Chí Minh"
    assert jobs[0].posted_at_raw == "1 ngày trước"
    assert jobs[0].source_url == "https://jobsgo.vn/viec-lam/ai-intern-123.html?ref_component=list"


def test_topdev_marks_internship_level_as_searchable(monkeypatch):
    html = """
    <div class="text-card-foreground">
      <a href="/detail-jobs/image-data-ai-123">Image Data AI</a>
      <span>ALCHERA VIỆT NAM</span><span>Hồ Chí Minh</span><span>2 days ago</span>
      <a href="/jobs/search?keyword=AI">AI</a>
    </div>
    """
    scraper = TopDevScraper(internship_only=True)
    get = Mock(return_value=response(html))
    monkeypatch.setattr(scraper, "_get", get)

    jobs = scraper.scrape("ai", max_pages=1)

    assert len(jobs) == 1
    assert jobs[0].job_type_raw == "internship"
    assert jobs[0].location == "Hồ Chí Minh"
    assert jobs[0].posted_at_raw == "2 days ago"
    assert get.call_args.kwargs["params"]["job_levels_ids"] == "1616"


def test_vietnamworks_reads_next_data(monkeypatch):
    hit = {
        "jobTitle": "AI Engineer Intern",
        "jobUrl": "https://www.vietnamworks.com/ai-engineer-intern-1-jv",
        "companyName": "Example Co",
        "workingLocations": [{"cityNameVI": "Hồ Chí Minh"}],
        "prettySalary": "Thương lượng",
        "approvedOn": "2026-09-22T10:00:00+07:00",
        "jobDescription": "Build AI prototypes",
        "jobRequirement": "Python",
        "skills": [{"skillName": "Machine Learning"}],
    }
    body = {"props": {"pageProps": {"seoSearch": {"searchResultData": {"hits": [hit]}}}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(body)}</script>'
    scraper = VietnamWorksScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response(html)))

    jobs = scraper.scrape("ai intern", max_pages=1)

    assert len(jobs) == 1
    assert jobs[0].title == "AI Engineer Intern"
    assert jobs[0].location == "Hồ Chí Minh"
    assert "Machine Learning" in jobs[0].description
