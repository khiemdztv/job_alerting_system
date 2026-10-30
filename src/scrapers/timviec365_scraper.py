"""
TimViec365.vn scraper.

The old scraper used ``/tim-kiem?keyword=`` — the site's free-text search.  That
page takes ~7.5 s to render (hence the ReadTimeout at 5–6 s), and robots.txt
disallows ``/tim-kiem``.  The category and "newest jobs" listings are allowed,
render in ~1.3–3.5 s and carry the same cards, so this scraper maps a keyword to
the closest TimViec365 category instead, and falls back to the newest-jobs
listing when no category fits (the caller's ranker then filters by keyword).

Verified URLs (30 Oct 2026):
- Newest jobs:     ``/viec-lam?page=N``                       (24 per page)
- Category:        ``/viec-lam-<cat-slug>-c<cat>v0``          (``/tim-viec-lam-ke-toan`` for c1)
- Category + city: ``/viec-lam-<cat-slug>-tai-<city-slug>-c<cat>v<city>``
- City only:       ``/viec-lam-tai-<city-slug>-c0v<city>``
                   (Hà Nội: ``/tim-viec-lam-tai-ha-noi.html``)

Card markup: ``div.item_vl[data-newid]`` with ``a.title_new`` (title/link),
``a.name_com`` (company), ``span.job_city``, ``span.job_money``,
``span.box_time_off`` (age of the posting, e.g. "5 giờ"; empty on pinned ads),
``p.item_catenew_exp`` (experience) and ``span.job_time`` (application deadline —
*not* the posting date, which the old parser mistakenly used).
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.common.text_utils import clean_vn_text, slugify_vn
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://timviec365.vn"
LATEST_URL = f"{BASE_URL}/viec-lam"

# (category id, category name as shown on the site).  Pages whose URL does not
# follow the c<id>v<city> pattern (Lương cao, Việc làm Tết, ...) are left out.
CATEGORIES: list[tuple[int, str]] = [
    (1, "Kế toán - Kiểm toán"), (2, "Hành chính - Văn phòng"), (3, "Sinh viên làm thêm"),
    (4, "Xây dựng"), (5, "Điện - Điện tử"), (6, "Làm bán thời gian"), (7, "Vận tải - Lái xe"),
    (8, "Khách sạn - Nhà hàng"), (9, "Nhân viên kinh doanh"), (10, "Bán hàng"),
    (11, "Cơ khí - Chế tạo"), (12, "Lao động phổ thông"), (13, "IT phần mềm"),
    (14, "Marketing - PR"), (17, "Giáo dục - Đào tạo"), (18, "Kỹ thuật"), (19, "Y tế - Dược"),
    (20, "Quản trị kinh doanh"), (21, "Dịch vụ"), (22, "Biên - Phiên dịch"),
    (23, "Dệt may - Da giày"), (24, "Kiến trúc - Tk nội thất"), (25, "Xuất - nhập khẩu"),
    (26, "IT Phần cứng - mạng"), (27, "Nhân sự"), (28, "Thiết kế - Mỹ thuật"), (29, "Tư vấn"),
    (30, "Bảo vệ"), (31, "Ô tô - xe máy"), (32, "Thư ký - Trợ lý"), (33, "Bất động sản"),
    (34, "Du lịch"), (35, "Báo chí - Truyền hình"), (36, "Thực phẩm - Đồ uống"),
    (38, "Vật tư - Thiết bị"), (39, "Thiết kế web"), (40, "In ấn - Xuất bản"),
    (41, "Nông - Lâm - Ngư - Nghiệp"), (42, "Thương mại điện tử"), (43, "Nhập liệu"),
    (44, "Việc làm thêm tại nhà"), (45, "Chăm sóc khách hàng"),
    (46, "Sinh viên mới tốt nghiệp - Thực tập"), (47, "Kỹ thuật ứng dụng"),
    (48, "Bưu chính viễn thông"), (49, "Dầu khí - Địa chất"),
    (50, "Giao thông vận tải - Thủy lợi - Cầu đường"), (51, "Khu chế xuất - Khu công nghiệp"),
    (52, "Làm đẹp - Thể lực - Spa"), (53, "Luật - Pháp lý"),
    (54, "Môi trường - Xử lý chất thải"), (55, "Mỹ phẩm - Thời trang - Trang sức"),
    (56, "Ngân hàng - Chứng khoán - Đầu tư"), (57, "Nghệ thuật - Điện ảnh"),
    (58, "Phát triển thị trường"), (59, "Phục vụ - Tạp vụ"), (60, "Quan hệ đối ngoại"),
    (61, "Quản lý điều hành"), (62, "Sản xuất - Vận hành sản xuất"),
    (63, "Thẩm định - Giám thẩm định - Quản lý chất lượng"), (64, "Thể dục - Thể thao"),
    (65, "Hóa học - Sinh học"), (66, "Bảo hiểm"), (67, "Freelancer"),
    (68, "Công chức - Viên chức"), (71, "Điện tử viễn thông"), (73, "Hoạch định - Dự án"),
    (77, "Tiếp thị - Quảng cáo"), (81, "Giúp việc"), (89, "Thủy sản"),
    (91, "Công nghệ thực phẩm"), (93, "Chăn nuôi - Thú y"), (95, "An toàn lao động"),
    (97, "Hàng không"), (101, "Tổ chức sự kiện"), (103, "Trắc địa"), (107, "Bảo trì"),
    (109, "Hàng hải"), (111, "Đầu bếp - phụ bếp"), (113, "Truyền thông"), (115, "Startup"),
    (119, "Thư viện"), (121, "Thống kê"), (123, "Copywriter"), (125, "Xuất khẩu lao động"),
    (127, "Công nghệ cao"), (129, "Logistic"), (131, "Tài chính"),
    (133, "Vận chuyển giao nhận"), (135, "Lễ tân - PG - PB"), (137, "Pha chế - Bar"),
    (139, "Quản lý đơn hàng"), (141, "Thu ngân"), (145, "Telesales"),
]
_CATEGORY_NAMES = dict(CATEGORIES)
# Listing paths that differ from the generic pattern (taken from the site's menu).
_CATEGORY_PATHS = {
    1: "/tim-viec-lam-ke-toan",
    10: "/viec-lam-ban-hang-c10v0",
    33: "/tim-viec-lam-bat-dong-san",
}
_CATEGORY_SLUGS = {10: "ban-hang", 33: "kd-bat-dong-san"}

# Common search words that name a category without sharing its wording.
KEYWORD_ALIASES: dict[str, int] = {
    "accountant": 1, "accounting": 1, "kiem toan": 1, "auditor": 1,
    "developer": 13, "lap trinh": 13, "software": 13, "python": 13, "java": 13,
    "backend": 13, "frontend": 13, "tester": 13, "data": 13, "ai": 13,
    "sales": 9, "sale": 9, "kinh doanh": 9, "hr": 27, "tuyen dung": 27,
    "marketing": 14, "designer": 28, "thiet ke": 28, "lai xe": 7, "tai xe": 7,
    "cong nhan": 12, "thuc tap": 46, "intern": 46, "internship": 46,
    "part time": 6, "ban thoi gian": 6, "logistics": 129, "xuat nhap khau": 25,
    "ngan hang": 56, "bank": 56, "phap ly": 53, "luat": 53, "legal": 53,
    "phien dich": 22, "bien dich": 22, "giao vien": 17, "teacher": 17,
    "ke toan": 1, "nhan su": 27, "cskh": 45, "customer service": 45,
}

# Province ids from https://timviec365.vn/sitemap-province.xml.
CITY_IDS: dict[str, int] = {
    "ha-noi": 1, "hai-phong": 2, "bac-giang": 3, "bac-kan": 4, "bac-ninh": 5,
    "cao-bang": 6, "dien-bien": 7, "hoa-binh": 8, "hai-duong": 9, "ha-giang": 10,
    "ha-nam": 11, "hung-yen": 12, "lao-cai": 13, "lai-chau": 14, "lang-son": 15,
    "ninh-binh": 16, "nam-dinh": 17, "phu-tho": 18, "quang-ninh": 19, "son-la": 20,
    "thai-binh": 21, "thai-nguyen": 22, "tuyen-quang": 23, "vinh-phuc": 24, "yen-bai": 25,
    "da-nang": 26, "thua-thien-hue": 27, "khanh-hoa": 28, "lam-dong": 29, "binh-dinh": 30,
    "binh-thuan": 31, "dak-lak": 32, "dak-nong": 33, "gia-lai": 34, "ha-tinh": 35,
    "kon-tum": 36, "nghe-an": 37, "ninh-thuan": 38, "phu-yen": 39, "quang-binh": 40,
    "quang-nam": 41, "quang-ngai": 42, "quang-tri": 43, "thanh-hoa": 44, "ho-chi-minh": 45,
    "binh-duong": 46, "ba-ria-vung-tau": 47, "can-tho": 48, "an-giang": 49, "bac-lieu": 50,
    "binh-phuoc": 51, "ben-tre": 52, "ca-mau": 53, "dong-thap": 54, "dong-nai": 55,
    "hau-giang": 56, "kien-giang": 57, "long-an": 58, "soc-trang": 59, "tien-giang": 60,
    "tay-ninh": 61, "tra-vinh": 62, "vinh-long": 63,
}
_CITY_ALIASES = {
    "hcm": "ho-chi-minh", "tphcm": "ho-chi-minh", "sai-gon": "ho-chi-minh",
    "saigon": "ho-chi-minh", "hn": "ha-noi", "hue": "thua-thien-hue",
    "vung-tau": "ba-ria-vung-tau",
}

_NO_RESULTS = re.compile(r"không tìm thấy|chưa có tin|không có (?:kết quả|việc làm)", re.I)


def city_slug(location: str | None) -> str:
    slug = slugify_vn(location or "")
    slug = re.sub(r"^(?:thanh-pho|tp|tinh)-", "", slug)
    return _CITY_ALIASES.get(slug, slug)


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", clean_vn_text(text))


def _contains(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def match_category(keyword: str) -> int | None:
    """Pick the TimViec365 category that best fits a free-text keyword."""
    words = _tokens(keyword)
    if not words:
        return None
    best: tuple[int, int] | None = None  # (matched words, category id)
    for cat_id, name in CATEGORIES:
        for part in name.split(" - "):
            part_words = _tokens(part)
            if _contains(words, part_words) or _contains(part_words, words):
                score = min(len(part_words), len(words))
                if best is None or score > best[0]:
                    best = (score, cat_id)
    if best:
        return best[1]
    for alias, cat_id in sorted(KEYWORD_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if _contains(words, alias.split()):
            return cat_id
    return None


def listing_url(category: int | None, city: int | None, city_name: str = "") -> str:
    if category is None and city is None:
        return LATEST_URL
    if city is None:
        return BASE_URL + _CATEGORY_PATHS.get(
            category, f"/viec-lam-{_category_slug(category)}-c{category}v0"
        )
    if category is None:
        if city == 1:
            return f"{BASE_URL}/tim-viec-lam-tai-ha-noi.html"
        return f"{BASE_URL}/viec-lam-tai-{city_name}-c0v{city}"
    return f"{BASE_URL}/viec-lam-{_category_slug(category)}-tai-{city_name}-c{category}v{city}"


def _category_slug(category: int) -> str:
    return _CATEGORY_SLUGS.get(category) or slugify_vn(_CATEGORY_NAMES[category])


def _canonical(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class TimViec365Scraper(BaseScraper):
    """Scraper for TimViec365.vn (static HTML, multi-industry job board)."""

    def __init__(self):
        super().__init__(source=JobSource.TIMVIEC365)

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: str | None = None,
    ) -> list[RawJob]:
        """Read the category listing that best matches ``keyword``.

        Keywords with no matching category read the newest-jobs listing, so the
        result is broader than the keyword; callers rank/filter afterwards.
        """
        city = self._city(location)
        url = listing_url(
            match_category(keyword), city[1] if city else None, city[0] if city else ""
        )
        return self._scrape_listing(url, max_pages)

    def scrape_latest(
        self, max_pages: Optional[int] = None, location: str | None = None
    ) -> list[RawJob]:
        """Newest postings across every category."""
        city = self._city(location)
        return self._scrape_listing(
            listing_url(None, city[1] if city else None, city[0] if city else ""), max_pages
        )

    @staticmethod
    def _city(location: str | None) -> tuple[str, int] | None:
        slug = city_slug(location) if location else ""
        return (slug, CITY_IDS[slug]) if slug in CITY_IDS else None

    def _scrape_listing(self, url: str, max_pages: Optional[int]) -> list[RawJob]:
        max_pages = 1 if self.live_mode else (max_pages or self.settings.scrape_max_pages)
        all_jobs: list[RawJob] = []
        seen: set[str] = set()

        for page in range(1, max_pages + 1):
            try:
                response = self._get(url, params={"page": page} if page > 1 else None)
            except (requests.RequestException, TimeoutError) as exc:
                logger.warning(f"TimViec365 page {page} failed: {exc}")
                break

            soup = BeautifulSoup(response.text, "html.parser")
            cards = soup.select("div.item_vl")
            jobs = [job for card in cards if (job := self.parse_job(card))]
            if not jobs:
                if page == 1 and not _NO_RESULTS.search(soup.get_text(" ", strip=True)):
                    self.mark_layout_changed()
                    logger.warning(
                        "TimViec365 returned a page without recognisable job cards",
                        extra={"source": self.source.value},
                    )
                break

            new = [job for job in jobs if job.source_url not in seen]
            seen.update(job.source_url for job in new)
            all_jobs.extend(new)
            logger.info(
                f"TimViec365 page {page}: {len(new)} jobs found",
                extra={"source": self.source.value},
            )
            if not new:  # past the last page the site repeats the final one
                break

        return all_jobs

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        """Parse one ``div.item_vl`` card."""
        try:
            link = raw_data.select_one("a.title_new[href]")
            href = link.get("href") if link else raw_data.get("new-href")
            title = ""
            if link:
                title = (link.get("title") or "").strip() or link.get_text(" ", strip=True)
            title = title or (raw_data.get("data-newtitle") or "").strip()
            if not href or not title:
                return None

            company_el = raw_data.select_one("a.name_com")
            company = (
                (company_el.get("title") or company_el.get_text(" ", strip=True))
                if company_el
                else (raw_data.get("data-usc_company") or "")
            ).strip()

            def text(selector: str) -> str:
                el = raw_data.select_one(selector)
                return el.get_text(" ", strip=True) if el else ""

            age = text(".box_time_off")
            posted_at_raw = f"{age} trước" if age and "trước" not in age else age
            experience = text(".item_catenew_exp")
            deadline = text(".job_time")
            details = [f"Kinh nghiệm: {experience}" if experience else "",
                       f"Hạn nộp: {deadline}" if deadline else ""]

            return RawJob(
                title=title,
                company=company,
                location=text(".job_city"),
                salary_raw=text(".job_money"),
                description=". ".join(d for d in details if d),
                source=JobSource.TIMVIEC365,
                source_url=_canonical(urljoin(BASE_URL, href)),
                posted_at_raw=posted_at_raw,
            )

        except Exception as e:
            logger.warning(f"Failed to parse TimViec365 job: {e}")
            return None
