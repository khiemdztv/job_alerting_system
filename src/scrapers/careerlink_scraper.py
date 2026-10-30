"""
CareerLink.vn scraper.

CareerLink moved to a Next.js front end in 2026; the old ``li.job-item`` markup
is gone, which is why the previous parser returned 0 jobs from a 200 page.
The listing is still server-rendered, so plain HTML parsing works.

Verified markup (30 Oct 2026, ``/vieclam/list``):
- Card: ``li[data-testid="job-result-row"]`` (CSS-module class ``JobIndex-module__<hash>__card``;
  the hash changes between deploys, so classes are matched by their ``__name`` suffix only)
- Title/link: ``a[class$="__jobLink"]`` (``title`` attribute), href ``/tim-viec-lam/<slug>/<id>``
- Company: ``a[class$="__company"]``; cities: links inside ``[class$="__location"]``
- Salary: ``[class$="__salary"]``; level: ``[class$="__careerLevel"]``
- Date: ``<time datetime="...Z">`` — with ``order=posted_date`` it is the posting date
  (label "Đăng"); with the default order it is the last-updated date ("Cập nhật").
- Result count: ``p[class$="__count"] > strong`` (``0`` on a genuine no-result page)

Query: ``/vieclam/list?keyword=<kw>&order=posted_date&sort=desc&page=N`` (50 per page);
city filter is a path: ``/tim-viec-lam-tai/<city-slug>/<CODE>?keyword=...``.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.common.text_utils import slugify_vn
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://www.careerlink.vn"

# City path segments as linked from CareerLink's own listing pages.
CITY_PATHS: dict[str, tuple[str, str]] = {
    "an-giang": ("an-giang", "AG"),
    "bac-ninh": ("bac-ninh", "BN"),
    "binh-duong": ("binh-duong", "BD"),
    "can-tho": ("can-tho", "CT"),
    "da-nang": ("da-nang", "DN"),
    "dien-bien": ("dien-bien", "DB"),
    "dong-nai": ("dong-nai", "DNI"),
    "dong-thap": ("dong-thap", "DT"),
    "gia-lai": ("gia-lai", "GL"),
    "ha-nam": ("ha-nam", "HNM"),
    "ha-noi": ("ha-noi", "HN"),
    "hai-phong": ("hai-phong", "HP"),
    "hau-giang": ("hau-giang", "HAG"),
    "ho-chi-minh": ("ho-chi-minh", "HCM"),
    "hue": ("hue", "TTH"),
    "hung-yen": ("hung-yen", "HY"),
    "khanh-hoa": ("khanh-hoa", "KH"),
    "kien-giang": ("kien-giang", "KG"),
    "lang-son": ("lang-son", "LS"),
    "long-an": ("long-an", "LA"),
    "ninh-binh": ("ninh-binh", "NB"),
    "phu-tho": ("phu-tho", "PT"),
    "quang-nam": ("quang-nam", "QN"),
    "quang-ninh": ("quang-ninh", "QNH"),
    "quang-tri": ("quang-tri", "QT"),
    "tay-ninh": ("tay-ninh", "TN"),
    "thai-binh": ("thai-binh", "TB"),
    "thai-nguyen": ("thai-nguyen", "TNN"),
    "tien-giang": ("tien-giang", "TG"),
}

_CITY_ALIASES = {
    "hcm": "ho-chi-minh", "tphcm": "ho-chi-minh", "sai-gon": "ho-chi-minh",
    "saigon": "ho-chi-minh", "hn": "ha-noi", "thua-thien-hue": "hue",
}


def city_slug(location: str | None) -> str:
    """Reduce "TP. Hồ Chí Minh", "HCM", "Hà Nội" ... to a bare city slug."""
    slug = slugify_vn(location or "")
    slug = re.sub(r"^(?:thanh-pho|tp|tinh)-", "", slug)
    return _CITY_ALIASES.get(slug, slug)


def _cls(name: str) -> re.Pattern:
    """Match a CSS-module class by its stable ``__name`` suffix."""
    return re.compile(rf"__{name}$")


def _canonical(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class CareerLinkScraper(BaseScraper):
    """Scraper for CareerLink.vn (no Cloudflare, server-rendered HTML)."""

    SEARCH_URL = f"{BASE_URL}/vieclam/list"

    def __init__(self):
        super().__init__(source=JobSource.CAREERLINK)

    def _listing_url(self, location: str | None) -> str:
        city = CITY_PATHS.get(city_slug(location)) if location else None
        if city:
            return f"{BASE_URL}/tim-viec-lam-tai/{city[0]}/{city[1]}"
        return self.SEARCH_URL

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: str | None = None,
    ) -> list[RawJob]:
        """Search CareerLink, newest postings first.

        Args:
            keyword: Search keyword (sent as-is; the site handles diacritics).
            max_pages: Max pages (50 jobs each). Live mode always reads one page.
            location: Optional city ("Hà Nội", "HCM", ...); unknown cities are ignored.
        """
        params = {"keyword": keyword.strip(), "order": "posted_date", "sort": "desc"}
        return self._scrape_listing(self._listing_url(location), params, max_pages)

    def scrape_latest(
        self, max_pages: Optional[int] = None, location: str | None = None
    ) -> list[RawJob]:
        """Every new posting on the board, newest first (no keyword)."""
        params = {"order": "posted_date", "sort": "desc"}
        return self._scrape_listing(self._listing_url(location), params, max_pages)

    def _scrape_listing(self, url: str, params: dict, max_pages: Optional[int]) -> list[RawJob]:
        max_pages = 1 if self.live_mode else (max_pages or self.settings.scrape_max_pages)
        all_jobs: list[RawJob] = []

        for page in range(1, max_pages + 1):
            page_params = dict(params, page=page) if page > 1 else dict(params)
            try:
                response = self._get(url, params=page_params)
            except (requests.RequestException, TimeoutError) as exc:
                # _get already recorded the HTTP status in last_error.
                logger.warning(f"CareerLink page {page} failed: {exc}")
                break

            soup = BeautifulSoup(response.text, "html.parser")
            cards = soup.select('[data-testid="job-result-row"]') or soup.find_all(
                "li", class_=_cls("card")
            )
            jobs = [job for card in cards if (job := self.parse_job(card))]
            if not jobs:
                if page == 1 and not self._reports_no_results(soup):
                    self.mark_layout_changed()
                    logger.warning(
                        "CareerLink returned a page without recognisable job cards",
                        extra={"source": self.source.value},
                    )
                break

            all_jobs.extend(jobs)
            logger.info(
                f"CareerLink page {page}: {len(jobs)} jobs found",
                extra={"source": self.source.value},
            )
            if not soup.select_one('nav[aria-label="pagination"] a[rel="next"]'):
                break

        return all_jobs

    @staticmethod
    def _reports_no_results(soup: BeautifulSoup) -> bool:
        """True when the page itself says the search matched nothing."""
        count = soup.find(class_=_cls("count"))
        strong = count.find("strong") if count else None
        return bool(strong and strong.get_text(strip=True) == "0")

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        """Parse one ``job-result-row`` card."""
        try:
            link_el = raw_data.find("a", class_=_cls("jobLink"))
            if not link_el or not link_el.get("href"):
                return None
            title = (link_el.get("title") or "").strip() or link_el.get_text(" ", strip=True)
            if not title:
                return None

            company_el = raw_data.find("a", class_=_cls("company"))
            company = ""
            if company_el:
                company = company_el.get("title") or company_el.get_text(" ", strip=True)

            location_el = raw_data.find(class_=_cls("location"))
            city_links = location_el.find_all("a") if location_el else []
            cities = [a.get_text(" ", strip=True) for a in city_links]
            location = ", ".join(c for c in cities if c) or (
                location_el.get_text(" ", strip=True) if location_el else ""
            )

            salary_el = raw_data.find(class_=_cls("salary"))
            salary = salary_el.get_text(" ", strip=True) if salary_el else ""

            level_el = raw_data.find(class_=_cls("careerLevel"))
            level = level_el.get_text(" ", strip=True) if level_el else ""

            time_el = raw_data.find("time", attrs={"datetime": True})
            posted_at_raw = time_el["datetime"] if time_el else ""

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                description=f"Cấp bậc: {level}" if level else "",
                job_type_raw=level,
                source=JobSource.CAREERLINK,
                source_url=_canonical(urljoin(BASE_URL, link_el["href"])),
                posted_at_raw=posted_at_raw,
            )

        except Exception as e:
            logger.warning(f"Failed to parse CareerLink job: {e}")
            return None
