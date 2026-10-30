import json
from unittest.mock import Mock

import requests

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
    # Tracking parameters are stripped so the same posting dedupes across searches.
    assert jobs[0].source_url == "https://jobsgo.vn/viec-lam/ai-intern-123.html"


def test_topdev_marks_internship_level_as_searchable(monkeypatch):
    # TopDev is read from its public JSON API (api.topdev.vn), not the slow HTML page.
    body = {
        "data": [
            {
                "id": 123,
                "title": "Image Data AI",
                "company": {"display_name": "ALCHERA VIỆT NAM"},
                "addresses": {"address_region_array": ["Thành phố Hồ Chí Minh"]},
                "detail_url": "https://topdev.vn/viec-lam/image-data-ai-123",
                "job_levels_str": "Thực tập",
                "published": {"date": "28-10-2026"},
            }
        ],
        "meta": {"last_page": 1},
    }
    result = Mock()
    result.json.return_value = body
    scraper = TopDevScraper(internship_only=True)
    get = Mock(return_value=result)
    monkeypatch.setattr(scraper, "_get", get)

    jobs = scraper.scrape("ai", max_pages=1)

    assert len(jobs) == 1
    assert jobs[0].job_type_raw.startswith("internship")
    assert jobs[0].location == "Hồ Chí Minh"
    assert jobs[0].posted_at_raw == "2026-10-28T00:00:00+07:00"
    assert get.call_args.kwargs["params"]["job_levels_ids"] == "1616"


def test_vietnamworks_falls_back_to_next_data_when_api_fails(monkeypatch):
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
    # The JSON search API is the primary path; an outage (not a block) falls
    # back to hits embedded in the search page's __NEXT_DATA__.
    monkeypatch.setattr(scraper, "_post", Mock(side_effect=requests.ConnectionError("down")))
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response(html)))

    jobs = scraper.scrape("ai intern", max_pages=1)

    assert len(jobs) == 1
    assert jobs[0].title == "AI Engineer Intern"
    assert jobs[0].location == "Hồ Chí Minh"
    assert "Machine Learning" in jobs[0].description


def test_jobsgo_reads_city_from_metadata_row_not_title(monkeypatch):
    html = """
    <div class="card job-card" data-id="1">
      <h3 class="job-title">
        <a href="/viec-lam/kinh-doanh-1.html" title="Nhân Viên Kinh Doanh Tại Hồ Chí Minh">
          <span style="color:white">HOT</span> Nhân Viên Kinh Doanh Tại Hồ Chí Minh
        </a>
      </h3>
      <a class="company-title" title="Stagro">Stagro</a>
      <div class="mt-1 text-primary fw-semibold small">
        <span>Từ 30 triệu VNĐ</span><span class="px-2">|</span><span>Hà Nội</span>
      </div>
      <div class="small">
        <span class="badge" title="Loại hình">Full-time</span>
        <span class="badge" title="Thời gian cập nhật">18 phút trước</span>
      </div>
    </div>
    """
    scraper = JobsGoScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response(html)))

    jobs = scraper.scrape("nhân viên kinh doanh", max_pages=1)

    assert len(jobs) == 1
    assert jobs[0].title == "Nhân Viên Kinh Doanh Tại Hồ Chí Minh"
    assert jobs[0].location == "Hà Nội"
    assert jobs[0].salary_raw == "Từ 30 triệu VNĐ"
    assert jobs[0].posted_at_raw == "18 phút trước"
