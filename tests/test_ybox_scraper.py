from pathlib import Path
from unittest.mock import Mock

from src.scrapers.base_scraper import LAYOUT_CHANGED
from src.scrapers.ybox_scraper import YBoxScraper

HTML = (Path(__file__).parent / "fixtures" / "ybox" / "list_page.html").read_text(
    encoding="utf-8"
)


def page(text):
    result = Mock()
    result.text = text
    return result


def scraper_with(monkeypatch, text=HTML):
    scraper = YBoxScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=page(text)))
    return scraper


def test_latest_returns_all_posts_newest_first(monkeypatch):
    jobs = scraper_with(monkeypatch).scrape_latest()
    assert len(jobs) >= 5
    dates = [j.posted_at_raw for j in jobs]
    assert dates == sorted(dates, reverse=True)
    assert all(j.source_url.startswith("https://ybox.vn/tuyen-dung/") for j in jobs)


def test_fields_from_real_post(monkeypatch):
    jobs = {j.company: j for j in scraper_with(monkeypatch).scrape_latest()}
    bavor = jobs["Công Ty Tư Vấn Bavor Consulting"]
    assert bavor.location == "HN"
    assert bavor.salary_raw == "8.000.000 - 15.000.000 VNĐ/Tháng"
    assert bavor.job_type_raw == "part-time, full-time"
    assert bavor.posted_at_raw == "2026-10-14T15:51:00+07:00"


def test_keyword_filter_and_location(monkeypatch):
    scraper = scraper_with(monkeypatch)
    jobs = scraper.scrape("thực tập sinh marketing")
    assert jobs and all("ybox.vn" in j.source_url for j in jobs)
    hn = scraper.scrape("thực tập", location="Hà Nội")
    assert hn and all(
        "HN" in j.location or "Online" in j.location or "Toàn Quốc" in j.location for j in hn
    )
    assert scraper.scrape("khinh khí cầu xyz") == []
    assert scraper.last_error == ""


def test_internship_detected():
    job = YBoxScraper().parse_job(
        {"title": "[HCM] ABC Tuyển Thực Tập Sinh Part-time", "slug": "abc", "publishedAt": ""}
    )
    assert job.job_type_raw == "internship, part-time"
    assert job.salary_raw == ""


def test_missing_payload_marks_layout_changed(monkeypatch):
    scraper = scraper_with(monkeypatch, "<html><script>var x=1</script></html>")
    assert scraper.scrape("kế toán") == []
    assert scraper.last_error == LAYOUT_CHANGED
