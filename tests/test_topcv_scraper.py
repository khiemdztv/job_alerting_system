from src.common.models import JobSource
from src.scrapers.topcv_scraper import TOPCV_DOMAINS, TopCVScraper


class FakeYouClient:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self.results


def test_topcv_keeps_only_job_postings_from_the_index():
    client = FakeYouClient([
        {
            "title": "Kế Toán Tổng Hợp - Công ty ABC",
            "company": "Công ty ABC",
            "location": "Hồ Chí Minh",
            "description": "Lương 12-15 triệu",
            "source_url": "https://www.topcv.vn/viec-lam/ke-toan-tong-hop/123456.html",
            "posted_at": "2026-10-28T03:00:00Z",
        },
        {
            "title": "Tuyển dụng Kế toán mới nhất",
            "source_url": "https://www.topcv.vn/tim-viec-lam-ke-toan",
        },
    ])
    jobs = TopCVScraper(client=client).scrape("kế toán")

    assert client.calls[0][0] == "kế toán"
    assert client.calls[0][1]["domains"] == TOPCV_DOMAINS
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == JobSource.TOPCV
    assert job.company == "Công ty ABC"
    assert job.location == "Hồ Chí Minh"
    assert job.posted_at_raw == "2026-10-28T03:00:00Z"


def test_topcv_cleans_title_url_and_extracts_salary():
    job = TopCVScraper(client=FakeYouClient([])).parse_job({
        "title": "Kế Toán Trưởng - Công ty XYZ | TopCV",
        "description": "Mức lương: 15 - 20 triệu. Địa điểm Hà Nội",
        "source_url": "https://www.topcv.vn/viec-lam/ke-toan-truong/99.html?ta_source=Search&u=1",
    })
    assert job.title == "Kế Toán Trưởng - Công ty XYZ"
    assert job.source_url == "https://www.topcv.vn/viec-lam/ke-toan-truong/99.html"
    assert job.salary_raw == "15 - 20 triệu"
