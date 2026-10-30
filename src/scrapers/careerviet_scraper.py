"""
CareerViet.vn scraper.

CareerViet (formerly CareerBuilder Vietnam) is a large multi-industry job board
served as Next.js SSR HTML; the first 50 cards are in the initial response.

URL grammar (verified 2026-10-30, the same paths the site's own links use):
- keyword:            /viec-lam/{kw}-k-sortdv-vi.html
- keyword + city:     /viec-lam/{kw}-tai-{city}-kl{id}-sortdv-vi.html
- city only:          /viec-lam/{city}-l{id}-sortdv-vi.html
- everything:         /viec-lam/tat-ca-viec-lam-sortdv-vi.html
- ``sortdv`` = newest "Cập nhật" first; page N inserts ``-trang-N`` before ``-vi``.
  ``?page=N`` is ignored by the server (it returns page 1 again).

Card selectors:
- Job container: div.job-item
- Title / link: a.job_link (title attr, href)
- Company: a.company-name
- Salary: div.salary
- Location: div.location li (one per city)
- Dates: div.time li -> "Hạn nộp: <time>" (deadline) and "Cập nhật: <time>" (last update)
- Categories: ul.welfare li
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.common.text_utils import clean_vn_text, slugify_vn
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://careerviet.vn"
LAYOUT_CHANGED = "layout_changed"
PAGE_SIZE = 50

# City slug -> CareerViet location id, from the footer links of the search page.
CITY_IDS: dict[str, int] = {
    "an-giang": 76, "ba-ria-vung-tau": 64, "bac-giang": 240, "bac-lieu": 781,
    "bac-ninh": 241, "ben-tre": 75, "binh-dinh": 56, "binh-duong": 650, "binh-phuoc": 651,
    "binh-thuan": 62, "ca-mau": 78, "can-tho": 71, "cao-bang": 26, "da-nang": 511,
    "dak-lak": 50, "dien-bien": 900, "dong-nai": 61, "dong-thap": 67, "gia-lai": 59,
    "ha-giang": 19, "ha-nam": 351, "ha-noi": 4, "ha-tinh": 39, "hai-duong": 320,
    "hai-phong": 31, "hau-giang": 780, "ho-chi-minh": 8, "hoa-binh": 18, "hung-yen": 321,
    "khanh-hoa": 58, "kien-giang": 77, "kon-tum": 60, "lam-dong": 63, "long-an": 72,
    "nam-dinh": 350, "nghe-an": 38, "ninh-binh": 30, "ninh-thuan": 68, "phu-tho": 210,
    "quang-nam": 510, "quang-ngai": 55, "soc-trang": 79, "tay-ninh": 66, "thai-binh": 36,
    "thai-nguyen": 280, "thanh-hoa": 37, "thua-thien-hue": 54, "tien-giang": 73,
    "tra-vinh": 74, "vinh-long": 70, "vinh-phuc": 211,
}
_CITY_ALIASES = {
    "hcm": "ho-chi-minh", "tphcm": "ho-chi-minh", "sai-gon": "ho-chi-minh",
    "saigon": "ho-chi-minh", "hn": "ha-noi", "hue": "thua-thien-hue",
}
_COUNT_RE = re.compile(r"([\d.,]+)\s*việc làm", re.IGNORECASE)
_DAYS_LEFT_RE = re.compile(r"con\s*:?\s*(\d+)\s*ngay")
VN_TZ = timezone(timedelta(hours=7))


def city_slug(location: str | None) -> str:
    """Reduce "TP. Hồ Chí Minh", "HCM", "Hà Nội" ... to a bare city slug."""
    slug = slugify_vn(location or "")
    for prefix in ("thanh-pho-", "tp-", "tinh-"):
        slug = slug.removeprefix(prefix)
    return _CITY_ALIASES.get(slug, slug)


def _canonical(href: str) -> str:
    parts = urlsplit(urljoin(BASE_URL, href))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class CareerVietScraper(BaseScraper):
    """Scraper for CareerViet.vn (SSR Next.js, multi-industry job board)."""

    def __init__(self):
        super().__init__(source=JobSource.CAREERVIET)

    # ── URLs ────────────────────────────────────────────────────

    def _build_search_url(
        self, keyword: str, page: int, location: str | None = None, newest: bool = True
    ) -> str:
        """Build a CareerViet listing URL (newest first unless ``newest=False``).

        Keyword is slugified ('Kế Toán' -> 'ke-toan'); the server matches it
        without diacritics exactly as it does the accented form.  An empty
        keyword lists every job.  Unknown locations are ignored.
        """
        keyword_slug = slugify_vn(keyword)
        city = city_slug(location)
        city_id = CITY_IDS.get(city)
        if location and city_id is None:
            logger.info("CareerViet has no location id for %r; searching all cities", location)
        if keyword_slug and city_id is not None:
            stem = f"{keyword_slug}-tai-{city}-kl{city_id}"
        elif keyword_slug:
            stem = f"{keyword_slug}-k"
        elif city_id is not None:
            stem = f"{city}-l{city_id}"
        else:
            stem = "tat-ca-viec-lam"
        sort = "-sortdv" if newest else ""
        paging = f"-trang-{page}" if page > 1 else ""
        return f"{BASE_URL}/viec-lam/{stem}{sort}{paging}-vi.html"

    # ── public entry points ─────────────────────────────────────

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: str | None = None,
        newest: bool | None = None,
    ) -> list[RawJob]:
        """Scrape CareerViet listings for a keyword.

        ``newest`` defaults to True in batch runs and False in live mode: one
        relevance-sorted page has ~2x more on-topic titles than the newest page.
        """
        if newest is None:
            newest = not self.live_mode
        return self._scrape_listing(keyword, max_pages, location, newest)

    def scrape_latest(
        self, max_pages: Optional[int] = None, location: str | None = None
    ) -> list[RawJob]:
        """Every job on CareerViet, newest "Cập nhật" first, no keyword."""
        return self._scrape_listing("", max_pages, location, True)

    def _scrape_listing(
        self, keyword: str, max_pages: Optional[int], location: str | None, newest: bool
    ) -> list[RawJob]:
        max_pages = max_pages or self.settings.scrape_max_pages
        all_jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        for page in range(1, max_pages + 1):
            url = self._build_search_url(keyword, page, location, newest)
            try:
                response = self._get(url)
                soup = BeautifulSoup(response.text, "html.parser")
                job_items = soup.find_all("div", class_="job-item")

                if not job_items:
                    if page == 1 and self._looks_broken(soup):
                        self.last_error = LAYOUT_CHANGED
                    logger.info(f"No more jobs on page {page}, stopping.")
                    break

                ids = {item.get("id", "") for item in job_items}
                if ids <= seen_ids:  # server repeated a page instead of advancing
                    break
                seen_ids |= ids

                parsed = [job for item in job_items if (job := self.parse_job(item))]
                if not parsed:
                    self.last_error = LAYOUT_CHANGED
                    break
                all_jobs.extend(parsed)
                logger.info(
                    f"CareerViet page {page}: {len(job_items)} jobs found",
                    extra={"source": self.source.value},
                )
                if len(job_items) < PAGE_SIZE:
                    break

            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    logger.info(f"CareerViet returned 404 for keyword '{keyword}', no results.")
                    self.last_error = ""
                else:
                    logger.error(f"CareerViet page {page} failed: {e}")
                break
            except (requests.RequestException, TimeoutError) as e:
                logger.error(f"CareerViet page {page} failed: {e}")
                break

        return all_jobs

    @staticmethod
    def _looks_broken(soup: BeautifulSoup) -> bool:
        """0 cards is fine on a real "no results" page, not when the page claims hits.

        A genuine empty search renders the ``job-found`` header as a loading
        skeleton with no <h1>; a results page has "<h1>5,018 việc làm ...</h1>".
        """
        if soup.find("div", class_="job-found") is None:
            return True
        heading = soup.find("h1")
        match = _COUNT_RE.search(heading.get_text(" ", strip=True)) if heading else None
        return bool(match and int(re.sub(r"\D", "", match.group(1)) or 0) > 0)

    # ── parsing ─────────────────────────────────────────────────

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        """Parse a CareerViet job-item element.

        <div class="job-item" id="job-item-35C773A3">
          <a class="job_link" title="Job Title" href="/vi/tim-viec-lam/x.35C773A3.html">
          <a class="company-name" title="Company">
          <div class="salary"><p>Lương: 20 Tr - 30 Tr VND</p></div>
          <div class="location"><ul><li>Hồ Chí Minh</li><li>Hà Nội</li></ul></div>
          <div class="time"><ul>
            <li><span>Hạn nộp: </span><time>12-11-2026</time></li>
            <li><span>Cập nhật:</span> <time>13-10-2026</time></li>
          </ul></div>
          <ul class="welfare"><li>Kế toán / Kiểm toán</li></ul>
        </div>
        """
        try:
            link_el = raw_data.find("a", class_="job_link")
            if not link_el:
                return None

            title = (link_el.get("title", "") or link_el.get_text(" ", strip=True)).strip()
            if not title:
                return None

            href = link_el.get("href", "")
            source_url = _canonical(href) if href else ""

            company_el = raw_data.find("a", class_="company-name")
            company = ""
            if company_el:
                company = company_el.get("title", "") or company_el.get_text(strip=True)

            salary = ""
            salary_el = raw_data.find("div", class_="salary")
            if salary_el:
                salary_text = salary_el.get_text(" ", strip=True)
                salary = re.sub(
                    r"^(Lương|Luong)\s*:\s*", "", salary_text, flags=re.IGNORECASE
                ).strip()

            location = ""
            loc_el = raw_data.find("div", class_="location")
            if loc_el:
                cities = [li.get_text(" ", strip=True) for li in loc_el.find_all("li")]
                if not cities and loc_el.get("title"):
                    cities = [part.strip() for part in loc_el["title"].split("|")]
                if not cities:
                    cities = [loc_el.get_text(" ", strip=True)]
                location = ", ".join(dict.fromkeys(city for city in cities if city))

            # "Hạn nộp" is the application DEADLINE, never a posting date.
            # "Cập nhật" is the date the employer last posted/refreshed the
            # job; the board sorts by it, so it is the closest real posted date.
            posted_at = deadline = ""
            time_el = raw_data.find("div", class_="time")
            if time_el:
                for li in time_el.find_all("li"):
                    label = clean_vn_text(li.get_text(" ", strip=True))
                    stamp = li.find("time")
                    if stamp and "cap nhat" in label:
                        posted_at = stamp.get_text(strip=True)
                    elif stamp and "han nop" in label:
                        deadline = stamp.get_text(strip=True)
                    elif days_left := _DAYS_LEFT_RE.search(label):
                        # "Cơ hội ứng tuyển chỉ còn: 3 ngày" replaces the date near the end.
                        end = datetime.now(VN_TZ) + timedelta(days=int(days_left.group(1)))
                        deadline = end.strftime("%d-%m-%Y")

            categories = [
                li.get("title") or li.get_text(" ", strip=True)
                for li in raw_data.select("ul.welfare li")
            ]
            description = f"Ngành: {', '.join(categories)}" if categories else ""

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                description=description,
                source=JobSource.CAREERVIET,
                source_url=source_url,
                posted_at_raw=posted_at,
                valid_through_raw=deadline,
            )

        except Exception as e:
            logger.warning(f"Failed to parse CareerViet job: {e}")
            return None
