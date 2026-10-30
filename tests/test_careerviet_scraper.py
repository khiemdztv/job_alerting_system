from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from bs4 import BeautifulSoup

from src.scrapers.careerviet_scraper import CareerVietScraper, city_slug

FIXTURES = Path(__file__).parent / "fixtures" / "careerviet"


def response(text):
    result = Mock()
    result.text = text
    return result


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_reads_real_search_cards(monkeypatch):
    scraper = CareerVietScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response(fixture("search.html"))))

    jobs = scraper.scrape("kế toán", max_pages=1)

    assert len(jobs) == 3
    first = jobs[0]
    assert first.title == "Kế Toán"
    assert first.company == "Công ty Cổ phần Homeflow"
    assert first.salary_raw == "Trên 16 Triệu VND"
    assert first.location == "Hồ Chí Minh"
    assert first.posted_at_raw == "13-10-2026"  # "Cập nhật", not the deadline
    assert first.valid_through_raw == "12-11-2026"  # "Hạn nộp"
    assert first.source_url == "https://careerviet.vn/vi/tim-viec-lam/ke-toan.35C83573.html"
    assert first.description.startswith("Ngành: Kế toán / Kiểm toán")
    assert jobs[2].location == "Bình Dương, Hồ Chí Minh, Đồng Nai"


def test_days_left_becomes_a_deadline_date():
    html = """
    <div class="job-item" id="job-item-1">
      <a class="job_link" title="Kế toán" href="/vi/tim-viec-lam/ke-toan.1.html"></a>
      <div class="time"><ul>
        <li><span>Cơ hội ứng tuyển chỉ còn : </span>3 ngày</li>
        <li><span>Cập nhật:</span> <time>21-10-2026</time></li>
      </ul></div>
    </div>
    """
    item = BeautifulSoup(html, "html.parser").find("div", class_="job-item")

    job = CareerVietScraper().parse_job(item)

    expected = datetime.now(timezone(timedelta(hours=7))) + timedelta(days=3)
    assert job.valid_through_raw == expected.strftime("%d-%m-%Y")
    assert job.posted_at_raw == "21-10-2026"


def test_url_grammar():
    scraper = CareerVietScraper()
    base = "https://careerviet.vn/viec-lam/"

    assert scraper._build_search_url("Kế Toán", 1) == base + "ke-toan-k-sortdv-vi.html"
    assert scraper._build_search_url("kế toán", 2) == base + "ke-toan-k-sortdv-trang-2-vi.html"
    assert (
        scraper._build_search_url("kế toán", 2, "TP.HCM")
        == base + "ke-toan-tai-ho-chi-minh-kl8-sortdv-trang-2-vi.html"
    )
    assert scraper._build_search_url("", 1, "Hà Nội") == base + "ha-noi-l4-sortdv-vi.html"
    assert scraper._build_search_url("", 3) == base + "tat-ca-viec-lam-sortdv-trang-3-vi.html"
    assert scraper._build_search_url("kế toán", 1, newest=False) == base + "ke-toan-k-vi.html"
    assert scraper._build_search_url("kế toán", 1, "Atlantis") == base + "ke-toan-k-sortdv-vi.html"


def test_live_mode_keeps_relevance_order(monkeypatch):
    scraper = CareerVietScraper()
    scraper.use_live_budget(None, 3.0)
    get = Mock(return_value=response(fixture("search.html")))
    monkeypatch.setattr(scraper, "_get", get)

    scraper.scrape("kế toán", max_pages=1)

    assert get.call_args.args[0] == "https://careerviet.vn/viec-lam/ke-toan-k-vi.html"


def test_scrape_latest_paginates_with_trang_and_stops_on_repeat(monkeypatch):
    html = fixture("search.html")
    # A full page (50 cards) so the scraper asks for the next one.
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(id="jobs-side-list-content")
    template = str(soup.find("div", class_="job-item"))
    for index in range(47):
        container.append(BeautifulSoup(template.replace("35C83573", f"X{index}"), "html.parser"))
    page = str(soup)
    get = Mock(return_value=response(page))  # server repeats the same page
    scraper = CareerVietScraper()
    monkeypatch.setattr(scraper, "_get", get)

    jobs = scraper.scrape_latest(max_pages=5)

    assert len(jobs) == 50
    urls = [call.args[0] for call in get.call_args_list]
    assert urls == [
        "https://careerviet.vn/viec-lam/tat-ca-viec-lam-sortdv-vi.html",
        "https://careerviet.vn/viec-lam/tat-ca-viec-lam-sortdv-trang-2-vi.html",
    ]


def test_real_no_results_page_is_not_an_error(monkeypatch):
    scraper = CareerVietScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response(fixture("no_results.html"))))

    assert scraper.scrape("xqzvwkplm", max_pages=1) == []
    assert scraper.last_error == ""


def test_count_without_cards_is_layout_change(monkeypatch):
    html = '<div class="job-found"><h1>5,018 việc làm kế toán</h1></div><div class="new-card"></div>'
    scraper = CareerVietScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response(html)))

    assert scraper.scrape("kế toán", max_pages=1) == []
    assert scraper.last_error == "layout_changed"


def test_unknown_page_is_layout_change(monkeypatch):
    scraper = CareerVietScraper()
    monkeypatch.setattr(scraper, "_get", Mock(return_value=response("<html><body/></html>")))

    assert scraper.scrape("kế toán", max_pages=1) == []
    assert scraper.last_error == "layout_changed"


def test_city_slug_aliases():
    assert city_slug("HCM") == "ho-chi-minh"
    assert city_slug("TP. Hồ Chí Minh") == "ho-chi-minh"
    assert city_slug("Thành phố Hà Nội") == "ha-noi"
