"""GenericJsonLdScraper against trimmed real pages and fixture sitemaps (no network)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from src.common.models import JobSource
from src.scrapers.base_scraper import BaseScraper
from src.scrapers.generic_jsonld_scraper import (
    KNOWN_SITES,
    LAYOUT_CHANGED,
    GenericJsonLdScraper,
    JsonLdSite,
    format_salary,
    parse_sitemap,
)

FIXTURES = Path(__file__).parent / "fixtures" / "generic_jsonld"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
BASE = "https://jobs.example.vn"
JOB_PATTERN = r"/viec-lam/[^/]+-\d+\.html"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    def __init__(self, url: str, content: bytes, status: int = 200, content_type: str = ""):
        self.url = url
        self.content = content
        self.status_code = status
        self.headers = {"content-type": content_type} if content_type else {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")


class FakeWeb:
    """Stands in for ``BaseScraper._get``: serves fixtures by URL, records calls."""

    def __init__(self, scraper: BaseScraper, pages: dict[str, bytes | int]):
        self.scraper = scraper
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url, params=None, **kwargs):
        self.calls.append(url)
        page = self.pages.get(url, 404)
        if isinstance(page, int):
            response = requests.Response()
            response.status_code = page
            exc = requests.HTTPError(f"HTTP {page}", response=response)
            self.scraper.last_error = BaseScraper._error_label(exc)
            raise exc
        return FakeResponse(url, page)


SITE = JsonLdSite(
    label="Example",
    sitemap_urls=(f"{BASE}/sitemap_index.xml",),
    job_url_pattern=JOB_PATTERN,
    max_age_days=3,
    sitemap_filter=r"/jobs-\d+\.xml",
)

SITEMAP_PAGES = {
    f"{BASE}/sitemap_index.xml": fixture("sitemap_index.xml"),
    f"{BASE}/sitemaps/jobs-1.xml": fixture("jobs-1.xml"),
    f"{BASE}/sitemaps/jobs-2.xml.gz": fixture("jobs-2.xml.gz"),
    f"{BASE}/sitemaps/blog.xml": fixture("blog.xml"),
}
PYTHON_URL = f"{BASE}/viec-lam/lap-trinh-vien-python-2001.html?utm_source=sitemap&id=7"
DETAIL_PAGES = {
    PYTHON_URL: fixture("graph_detail.html"),
    f"{BASE}/viec-lam/ke-toan-tong-hop-1001.html": fixture("array_detail.html"),
    f"{BASE}/viec-lam/ke-toan-noi-bo-1004.html": fixture("og_only.html"),
    f"{BASE}/viec-lam/thu-ngan-1003.html": fixture("nested_detail.html"),
}


def make(site: JsonLdSite = SITE, pages: dict | None = None, **kwargs):
    scraper = GenericJsonLdScraper(site, now=lambda: NOW, **kwargs)
    web = FakeWeb(scraper, pages if pages is not None else {**SITEMAP_PAGES, **DETAIL_PAGES})
    scraper._get = web.get
    return scraper, web


# ── Discovery ────────────────────────────────────────────────────


def test_sitemap_index_recursion_filters_by_lastmod_and_pattern():
    scraper, web = make()
    urls = scraper._discover(None)

    # Newest child sitemap first (the gzipped one), blog sitemap never fetched.
    assert web.calls == [
        f"{BASE}/sitemap_index.xml",
        f"{BASE}/sitemaps/jobs-2.xml.gz",
        f"{BASE}/sitemaps/jobs-1.xml",
    ]
    assert urls == [
        PYTHON_URL,  # 2026-10-30
        f"{BASE}/viec-lam/ke-toan-tong-hop-1001.html",  # 2026-10-29, listed twice
        f"{BASE}/viec-lam/ke-toan-noi-bo-1004.html",  # 2026-10-28, CDATA loc
        f"{BASE}/viec-lam/thu-ngan-1003.html",  # no lastmod: eligible, last
    ]
    # 2026-09-15 is older than max_age_days; the category page fails the pattern.
    assert not any("nhan-vien-kho" in u or "tim-viec-lam-ke-toan" in u for u in urls)


def test_parse_sitemap_handles_gzip_and_entities():
    from src.scrapers.generic_jsonld_scraper import _decode_sitemap

    is_index, items = parse_sitemap(_decode_sitemap(fixture("jobs-2.xml.gz")))
    assert not is_index
    assert items[1][0] == PYTHON_URL  # &amp; unescaped
    assert items[1][1] == datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)

    is_index, children = parse_sitemap(fixture("sitemap_index.xml").decode())
    assert is_index and len(children) == 3


def test_scrape_latest_maps_every_json_ld_variant():
    scraper, _ = make()
    jobs = {job.title: job for job in scraper.scrape_latest()}

    python = jobs["Lập trình viên Python"]  # @graph, comment-wrapped, trailing commas
    assert python.company == "Công ty Rắn Vàng"
    assert python.location == "Quận 1, Hồ Chí Minh, Cầu Giấy, Hà Nội"
    assert python.salary_raw == "2000 USD/MONTH"
    assert python.job_type_raw == "FULL_TIME, CONTRACTOR"
    assert python.description == "Phát triển\nAPI\ncho hệ thống tuyển dụng"
    assert python.requirements == "Viết code Python\nReview code\n3 năm Python\nDjango"
    assert python.posted_at_raw == "2026-10-30"
    # No canonical link: fetched URL without tracking parameters.
    assert python.source_url == f"{BASE}/viec-lam/lap-trinh-vien-python-2001.html?id=7"

    accounting = jobs["Kế toán tổng hợp"]  # top-level array, relative canonical
    assert accounting.company == "Công ty ABC"
    assert accounting.location == "Đà Nẵng, Hà Nội"  # "Not Available" dropped
    assert accounting.salary_raw == "12000000 VND/MONTH"
    assert accounting.source_url == f"{BASE}/viec-lam/ke-toan-tong-hop-1001.html"

    cashier = jobs["Thu ngân ca tối"]  # JobPosting nested under WebPage.mainEntity
    assert cashier.company == "Siêu thị Mini"
    assert cashier.location == "Remote"
    assert cashier.salary_raw == "Thỏa thuận"

    og = jobs["Tuyển Kế toán nội bộ & thuế"]  # OpenGraph fallback
    assert og.description == "Công ty XYZ tuyển kế toán nội bộ tại Hà Nội"
    assert og.company == ""
    assert all(job.source == JobSource.GENERIC for job in jobs.values())
    assert scraper.last_error == ""


@pytest.mark.parametrize(
    ("salary", "expected"),
    [
        ({"currency": "VND", "value": {"minValue": 15000000, "maxValue": 20000000,
                                        "unitText": "MONTH"}}, "15000000-20000000 VND/MONTH"),
        ({"currency": "USD", "value": {"minValue": 800, "maxValue": 1500, "unitText": "MONTH",
                                        "value": "800 - 1,500 USD"}}, "800-1500 USD/MONTH"),
        ({"currency": "VND", "value": {"value": "Cạnh tranh", "unitText": "MONTH"}}, "Cạnh tranh"),
        ({"currency": "VND", "value": {"maxValue": "25,000,000"}}, "25000000 VND"),
        ({"currency": "USD", "value": 12.5, "unitText": "hour"}, "12.5 USD/HOUR"),
        ([{"currency": "VND", "value": {"value": 9000000}}], "9000000 VND"),
        ("Thương lượng", "Thương lượng"),
        (None, ""),
    ],
)
def test_salary_forms(salary, expected):
    assert format_salary(salary) == expected


# ── Real pages (trimmed fixtures captured 2026-10-30) ────────────


def parse_fixture(name: str, url: str):
    scraper = GenericJsonLdScraper(JsonLdSite(label="t"))
    return scraper.parse_page(fixture(name).decode("utf-8"), url)


def test_real_careerviet_page():
    job, had = parse_fixture("careerviet_detail.html", "https://careerviet.vn/x?utm_source=fb")
    assert had
    assert job.title == "NHÂN VIÊN BIẾT TIẾNG TRUNG - NHIỀU BỘ PHẬN"
    assert job.company == "CÔNG TY TNHH ELECTRONIC TRIPOD VIỆT NAM (CHÂU ĐỨC)"
    assert job.location == "Bà Rịa - Vũng Tàu, Thành phố Bà Rịa"
    assert job.salary_raw == "Cạnh tranh"
    assert job.job_type_raw == "FULL_TIME"  # board emits ['"FULL_TIME"']
    assert job.source_url.endswith("nhan-vien-biet-tieng-trung-nhieu-bo-phan.35C8C6A1.html")
    assert "<" not in job.description and len(job.description) <= 4000
    assert "Trung cấp/Cao đẳng" in job.requirements


def test_real_viecoi_page():
    job, _ = parse_fixture("viecoi_detail.html", "https://viecoi.vn/x")
    assert job.title == "Nhân viên Kinh doanh"
    assert job.salary_raw == "15000000-20000000 VND/MONTH"
    assert job.location == "Quảng Nam"
    assert job.posted_at_raw == "2026-10-29T16:49:17+07:00"
    assert job.description.startswith("CHI TIẾT CÔNG VIỆC:")


def test_real_careerlink_page_with_multiple_locations():
    job, _ = parse_fixture("careerlink_detail.html", "https://www.careerlink.vn/x")
    assert job.company == "CÔNG TY TNHH KINGKONG PQ (Kingkong Mart Phú Quốc)"
    assert job.location.startswith("Huyện Phú Quốc, Kiên Giang, Phú Quốc")
    assert job.salary_raw == "Thương lượng"
    assert job.source_url == "https://www.careerlink.vn/tim-viec-lam/giam-sat-kho-dc/3665474"


def test_real_itviec_page_single_quoted_attributes():
    job, _ = parse_fixture("itviec_detail.html", "https://itviec.com/x")
    assert job.title == "Senior C++ Developer"
    assert job.company == "DTS Software Vietnam"
    assert job.location == "Hà Nội"  # addressLocality "Not Available" dropped
    assert job.salary_raw == "800-1500 USD/MONTH"
    assert job.source_url == "https://itviec.com/viec-lam-it/senior-c-developer-dts-software-vietnam-1533"
    assert job.requirements.startswith("C++, Technical Writing")


def test_page_without_jobposting_or_metadata_is_skipped():
    job, had = parse_fixture("plain.html", "https://x.vn/a")
    assert job is None and not had


# ── Signals and budgets ──────────────────────────────────────────


def test_layout_changed_when_no_page_has_jobposting():
    pages = {
        **SITEMAP_PAGES,
        PYTHON_URL: fixture("og_only.html"),
        f"{BASE}/viec-lam/ke-toan-tong-hop-1001.html": fixture("plain.html"),
        f"{BASE}/viec-lam/ke-toan-noi-bo-1004.html": fixture("og_only.html"),
        f"{BASE}/viec-lam/thu-ngan-1003.html": 404,
    }
    scraper, _ = make(pages=pages)
    jobs = scraper.scrape_latest()

    assert scraper.last_error == LAYOUT_CHANGED
    assert [job.title for job in jobs] == ["Tuyển Kế toán nội bộ & thuế"] * 2


def test_single_404_does_not_mark_the_run_failed():
    pages = {**SITEMAP_PAGES, **DETAIL_PAGES, f"{BASE}/viec-lam/thu-ngan-1003.html": 404}
    scraper, _ = make(pages=pages)
    assert len(scraper.scrape_latest()) == 3
    assert scraper.last_error == ""


def test_live_mode_caps_sitemaps_and_detail_pages():
    scraper, web = make(live_max_detail_pages=2, live_max_sitemaps=2)
    scraper.use_live_budget(deadline_at=time.monotonic() + 30, timeout=4.0)
    jobs = scraper.scrape_latest()

    sitemap_calls = [u for u in web.calls if u in SITEMAP_PAGES]
    assert len(sitemap_calls) == 2  # index + newest child only
    assert len(web.calls) - len(sitemap_calls) == 2
    assert len(jobs) == 2


def test_spent_deadline_makes_no_requests():
    scraper, web = make()
    scraper.deadline_at = time.monotonic() - 1
    assert scraper.scrape_latest() == []
    assert web.calls == []


def test_blocked_site_stops_the_run():
    pages = {**SITEMAP_PAGES, **DETAIL_PAGES, PYTHON_URL: 403}
    scraper, web = make(pages=pages)
    assert scraper.scrape_latest() == []
    assert scraper.last_error == "HTTP403"
    assert web.calls[-1] == PYTHON_URL  # nothing fetched after the block


# ── Keyword modes ────────────────────────────────────────────────


def test_keyword_without_search_url_filters_locally_and_fetches_slug_matches_first():
    scraper, web = make()
    jobs = scraper.scrape("Kế toán")

    detail_calls = [u for u in web.calls if u not in SITEMAP_PAGES]
    assert detail_calls[:2] == [
        f"{BASE}/viec-lam/ke-toan-tong-hop-1001.html",
        f"{BASE}/viec-lam/ke-toan-noi-bo-1004.html",
    ]
    assert sorted(job.title for job in jobs) == ["Kế toán tổng hợp", "Tuyển Kế toán nội bộ & thuế"]


def test_search_url_collects_job_links_from_results():
    site = JsonLdSite(
        source=JobSource.CAREERLINK,
        label="CareerLink",
        search_url="https://www.careerlink.vn/vieclam/list?keyword={keyword}&page={page}",
        job_url_pattern=r"careerlink\.vn/tim-viec-lam/[^/]+/\d+$",
    )
    page1 = "https://www.careerlink.vn/vieclam/list?keyword=k%E1%BA%BF+to%C3%A1n&page=1"
    page2 = "https://www.careerlink.vn/vieclam/list?keyword=k%E1%BA%BF+to%C3%A1n&page=2"
    detail = "https://www.careerlink.vn/tim-viec-lam/giam-sat-kho-dc/3665474"
    pages = {
        page1: fixture("search_results.html"),
        page2: fixture("search_results.html"),  # nothing new → stop paging
        detail: fixture("careerlink_detail.html"),
    }
    scraper, web = make(site=site, pages=pages)
    jobs = scraper.scrape("kế toán", max_pages=3)

    assert web.calls[:2] == [page1, page2]
    assert detail in web.calls
    assert "https://www.careerlink.vn/tim-viec-lam/ho-chi-minh-nhan-vien-ke-toan/3665442" in web.calls
    assert len(web.calls) == 4  # 2 result pages + 2 unique job links
    assert [job.source for job in jobs] == [JobSource.CAREERLINK]


def test_listing_discovery_with_page_template():
    site = JsonLdSite(
        label="Listing",
        listing_urls=("https://www.careerlink.vn/vieclam/list?page={page}",),
        job_url_pattern=r"careerlink\.vn/tim-viec-lam/[^/]+/\d+$",
    )
    pages = {"https://www.careerlink.vn/vieclam/list?page=1": fixture("search_results.html")}
    scraper, web = make(site=site, pages=pages)
    urls = scraper._discover(max_pages=1)
    assert urls == [
        "https://www.careerlink.vn/tim-viec-lam/giam-sat-kho-dc/3665474",
        "https://www.careerlink.vn/tim-viec-lam/ho-chi-minh-nhan-vien-ke-toan/3665442",
    ]


def test_known_sites_are_well_formed():
    import re

    assert len(KNOWN_SITES) >= 2
    for site in KNOWN_SITES:
        assert site.source != JobSource.GENERIC
        assert site.sitemap_urls or site.listing_urls or site.search_url
        re.compile(site.job_url_pattern)
    samples = {
        "CareerViet": "https://careerviet.vn/vi/tim-viec-lam/ke-toan.35C8C4D7.html",
        "ViecLam24h": "https://vieclam24h.vn/ke-toan/ke-toan-truong-c17p210000188id200958490.html",
        "Viecoi": "https://viecoi.vn/viec-lam/nhan-vien-kinh-doanh-138757.html",
        "ITviec": "https://itviec.com/viec-lam-it/senior-c-developer-dts-software-vietnam-1533",
        "CareerLink": "https://www.careerlink.vn/tim-viec-lam/giam-sat-kho-dc/3665474",
        "123job": "https://123job.vn/viec-lam/ke-toan-cong-no-29GgoNXv9E",
    }
    by_label = {site.label: site for site in KNOWN_SITES}
    for label, url in samples.items():
        assert re.search(by_label[label].job_url_pattern, url), label
    assert not re.search(by_label["CareerViet"].job_url_pattern,
                         "https://careerviet.vn/en/search-job/ke-toan.35C8C4D7.html")
