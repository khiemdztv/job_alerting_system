"""
ViecLam24h.vn scraper.

The search page ``/tim-kiem-viec-lam-nhanh`` is Next.js SSR.  Its
``__NEXT_DATA__`` holds the 30 jobs of the page as JSON
(``props.initialProps.pageProps.jobsResponse``) with real Unix timestamps
(``approved_at``), salary range, all province ids and working method, plus the
province table needed to name them.  The HTML cards (``<a data-job-id>``)
give the display URL and salary text and are the fallback when the JSON is gone.

Query parameters (verified 2026-10-30, the ones the site's own UI sends):
- ``q``: keyword (UTF-8, diacritics kept)
- ``sort_q=actived_at_by_box,desc``: "Mới nhất" (default is ``priority_max,desc``)
- ``province_ids[]``: post-2025 province id (e.g. 210000187 = TP.HCM)
- ``page``: 1-based; the server fixes ``per_page`` at 30 and caps at ~34 pages
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.common.text_utils import slugify_vn
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://vieclam24h.vn"
LAYOUT_CHANGED = "layout_changed"
SORT_NEWEST = "actived_at_by_box,desc"
VN_TZ = timezone(timedelta(hours=7))

# Post-July-2025 provinces (34 units), as listed in the page's AuLocation table.
PROVINCE_IDS: dict[str, int] = {
    "ha-noi": 210000160, "cao-bang": 210000161, "tuyen-quang": 210000162,
    "dien-bien": 210000163, "lai-chau": 210000164, "son-la": 210000165,
    "lao-cai": 210000166, "thai-nguyen": 210000167, "lang-son": 210000168,
    "quang-ninh": 210000169, "bac-ninh": 210000170, "phu-tho": 210000171,
    "hai-phong": 210000172, "hung-yen": 210000173, "ninh-binh": 210000174,
    "thanh-hoa": 210000175, "nghe-an": 210000176, "ha-tinh": 210000177,
    "quang-tri": 210000178, "hue": 210000179, "da-nang": 210000180,
    "quang-ngai": 210000181, "gia-lai": 210000182, "khanh-hoa": 210000183,
    "dak-lak": 210000184, "lam-dong": 210000185, "dong-nai": 210000186,
    "ho-chi-minh": 210000187, "tay-ninh": 210000188, "dong-thap": 210000189,
    "vinh-long": 210000190, "an-giang": 210000191, "can-tho": 210000192,
    "ca-mau": 210000193, "nuoc-ngoai": 210000194,
}
_PROVINCE_NAMES = {
    210000187: "TP.HCM", 210000160: "Hà Nội", 210000180: "Đà Nẵng", 210000172: "Hải Phòng",
    210000192: "Cần Thơ", 210000186: "Đồng Nai", 210000170: "Bắc Ninh", 210000179: "Huế",
}
# Aliases and provinces merged in the 2025 reorganisation -> the surviving unit.
_PROVINCE_ALIASES = {
    "hcm": "ho-chi-minh", "tphcm": "ho-chi-minh", "tp-hcm": "ho-chi-minh",
    "sai-gon": "ho-chi-minh", "saigon": "ho-chi-minh", "hn": "ha-noi",
    "thua-thien-hue": "hue", "binh-duong": "ho-chi-minh", "ba-ria-vung-tau": "ho-chi-minh",
    "vung-tau": "ho-chi-minh", "hai-duong": "hai-phong", "quang-nam": "da-nang",
    "soc-trang": "can-tho", "hau-giang": "can-tho", "long-an": "tay-ninh",
    "binh-phuoc": "dong-nai", "ben-tre": "vinh-long", "tra-vinh": "vinh-long",
    "tien-giang": "dong-thap", "kien-giang": "an-giang", "bac-lieu": "ca-mau",
    "dak-nong": "lam-dong", "binh-thuan": "lam-dong", "phu-yen": "dak-lak",
    "ninh-thuan": "khanh-hoa", "binh-dinh": "gia-lai", "kon-tum": "quang-ngai",
    "quang-binh": "quang-tri", "bac-giang": "bac-ninh", "thai-binh": "hung-yen",
    "ha-nam": "ninh-binh", "nam-dinh": "ninh-binh", "vinh-phuc": "phu-tho",
    "hoa-binh": "phu-tho", "bac-kan": "thai-nguyen", "ha-giang": "tuyen-quang",
    "yen-bai": "lao-cai",
}
WORKING_METHOD = {
    1: "Toàn thời gian cố định", 2: "Toàn thời gian tạm thời", 3: "Bán thời gian cố định",
    4: "Bán thời gian tạm thời", 5: "Theo hợp đồng tư vấn", 6: "Thực tập", 7: "Khác",
}
_SALARY_RE = re.compile(r"triệu|VNĐ|VND|USD|thỏa thuận|thoả thuận|thương lượng", re.IGNORECASE)


def province_id(location: str | None) -> int | None:
    """Map "TP. Hồ Chí Minh", "HCM", "Bình Dương" ... to a current province id."""
    slug = slugify_vn(location or "")
    for prefix in ("thanh-pho-", "tp-", "tinh-"):
        slug = slug.removeprefix(prefix)
    return PROVINCE_IDS.get(_PROVINCE_ALIASES.get(slug, slug))


def _canonical(href: str) -> str:
    """Drop ``open_from``/``search_id`` tracking parameters."""
    parts = urlsplit(urljoin(BASE_URL, href))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _iso(timestamp: object) -> str:
    try:
        value = int(timestamp)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, tz=VN_TZ).isoformat()


def _format_salary(job: dict) -> str:
    low, high = int(job.get("salary_min") or 0), int(job.get("salary_max") or 0)
    if not low and not high:
        return "Thỏa thuận"
    if job.get("salary_unit") not in (0, None):  # non-VND amounts are rare; keep raw numbers
        return f"{low} - {high}" if low and high else str(high or low)

    def million(value: int) -> str:
        return f"{value / 1_000_000:g}"

    if low and high:
        return f"{million(low)} - {million(high)} triệu"
    return f"Từ {million(low)} triệu" if low else f"Đến {million(high)} triệu"


class ViecLam24hScraper(BaseScraper):
    """Scraper for ViecLam24h.vn (no Cloudflare, Next.js SSR)."""

    SEARCH_URL = f"{BASE_URL}/tim-kiem-viec-lam-nhanh"

    def __init__(self):
        super().__init__(source=JobSource.VIECLAM24H)
        self._province_names: dict[int, str] = dict(_PROVINCE_NAMES)
        self._occupation_slugs: dict[int, str] = {}

    # ── public entry points ─────────────────────────────────────

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: str | None = None,
        newest: bool | None = None,
    ) -> list[RawJob]:
        """Search by keyword.

        ``newest`` defaults to True in batch runs and False in live mode, where
        the single page read is better spent on the site's default ranking.
        """
        if newest is None:
            newest = not self.live_mode
        return self._scrape_listing(keyword, max_pages, location, newest)

    def scrape_latest(
        self, max_pages: Optional[int] = None, location: str | None = None
    ) -> list[RawJob]:
        """Every job, newest first, no keyword (server stops after ~34 pages)."""
        return self._scrape_listing("", max_pages, location, True)

    def _params(
        self, keyword: str, page: int, location: str | None, newest: bool = True
    ) -> dict:
        params: dict = {"sort_q": SORT_NEWEST, "page": page} if newest else {"page": page}
        if keyword.strip():
            params["q"] = keyword.strip()
        wanted = province_id(location)
        if wanted is not None:
            params["province_ids[]"] = wanted
        elif location:
            logger.info("ViecLam24h has no province id for %r; searching all", location)
        return params

    def _scrape_listing(
        self, keyword: str, max_pages: Optional[int], location: str | None, newest: bool
    ) -> list[RawJob]:
        max_pages = max_pages or self.settings.scrape_max_pages
        all_jobs: list[RawJob] = []

        for page in range(1, max_pages + 1):
            try:
                params = self._params(keyword, page, location, newest)
                response = self._get(self.SEARCH_URL, params=params)
                soup = BeautifulSoup(response.text, "html.parser")
                cards = {
                    str(card["data-job-id"]): card
                    for card in soup.find_all(attrs={"data-job-id": True})
                }
                listing = self._listing_data(soup)

                if listing is not None:
                    items = listing.get("items") or []
                    jobs = [job for item in items if (job := self._parse_item(item, cards))]
                    total_pages = int(listing.get("total_pages") or listing.get("last") or 0)
                else:
                    items = list(cards.values())
                    jobs = [job for card in items if (job := self.parse_job(card))]
                    total_pages = 0

                if not items:
                    if listing is None and page == 1:
                        self.last_error = LAYOUT_CHANGED  # neither JSON nor cards
                    logger.info(f"No more jobs on page {page}, stopping.")
                    break
                if not jobs:
                    self.last_error = LAYOUT_CHANGED
                    break

                all_jobs.extend(jobs)
                logger.info(
                    f"ViecLam24h page {page}: {len(items)} jobs",
                    extra={"source": self.source.value},
                )
                if total_pages and page >= total_pages:
                    break

            except (requests.RequestException, TimeoutError) as e:
                logger.error(f"ViecLam24h page {page} failed: {e}")
                break
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                logger.error(f"ViecLam24h page {page} has an unexpected shape: {e}")
                self.last_error = LAYOUT_CHANGED
                break

        return all_jobs

    # ── embedded JSON ───────────────────────────────────────────

    def _listing_data(self, soup: BeautifulSoup) -> dict | None:
        """Return ``jobsResponse`` and learn the page's lookup tables."""
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return None
        try:
            props = json.loads(script.string).get("props", {})
        except ValueError:
            return None
        state = props.get("initialState") or {}
        for province in (state.get("AuLocation") or {}).get("provinces") or []:
            if isinstance(province, dict) and province.get("id") and province.get("name"):
                self._province_names[int(province["id"])] = str(province["name"])
        common = ((state.get("api") or {}).get("initCommon") or {}).get("data") or {}
        for occupation in common.get("occupation") or []:
            if isinstance(occupation, dict) and occupation.get("id") and occupation.get("slug"):
                self._occupation_slugs[int(occupation["id"])] = str(occupation["slug"])

        listing = (props.get("initialProps") or {}).get("pageProps", {}).get("jobsResponse")
        if not isinstance(listing, dict):
            listing = ((state.get("api") or {}).get("getJobList") or {}).get("data")
        return listing if isinstance(listing, dict) and "items" in listing else None

    def _parse_item(self, item: dict, cards: dict[str, Tag]) -> Optional[RawJob]:
        if not isinstance(item, dict):
            return None
        title = str(item.get("title") or "").strip()
        job_id = str(item.get("id") or "")
        if not title or not job_id:
            return None
        card = cards.get(job_id)

        href = card.get("href", "") if card is not None else ""
        if not href:
            occupations = item.get("occupation_ids_main") or []
            provinces = item.get("province_ids") or []
            slug = self._occupation_slugs.get(occupations[0]) if occupations else None
            if slug and provinces and item.get("title_slug"):
                href = (
                    f"/{slug}/{item['title_slug']}-c{occupations[0]}p{provinces[0]}id{job_id}.html"
                )
        if not href:
            return None

        employer = item.get("employer_info") or {}
        cities = [
            self._province_names.get(int(pid), "")
            for pid in (item.get("province_ids") or [])
            if str(pid).isdigit()
        ]
        location = ", ".join(dict.fromkeys(city for city in cities if city))
        if not location and card is not None:
            location = self._card_location(card)

        salary = self._card_salary(card) if card is not None else ""
        tags = [
            str(tag.get("name"))
            for tag in (item.get("smart_tags") or [])
            if isinstance(tag, dict) and tag.get("name")
        ]
        requirements = BeautifulSoup(
            str(item.get("job_requirement_html") or item.get("job_requirement") or ""),
            "html.parser",
        ).get_text(" ", strip=True)

        return RawJob(
            title=title,
            company=str(employer.get("name") or "") if isinstance(employer, dict) else "",
            location=location,
            salary_raw=salary or _format_salary(item),
            description=f"Kỹ năng: {', '.join(tags)}" if tags else "",
            requirements=requirements[:1500],
            job_type_raw=WORKING_METHOD.get(item.get("working_method"), ""),
            source=JobSource.VIECLAM24H,
            source_url=_canonical(href),
            posted_at_raw=_iso(item.get("approved_at") or item.get("created_at")),
            valid_through_raw=_iso(item.get("resume_apply_expired")),
        )

    # ── HTML card fallback ──────────────────────────────────────

    @staticmethod
    def _card_salary(card: Tag) -> str:
        span = card.find("span", class_="text-[#2C95FF]")
        if span is None:
            span = card.find("span", string=_SALARY_RE)
        return span.get_text(strip=True) if span is not None else ""

    @staticmethod
    def _card_location(card: Tag) -> str:
        tooltip = [
            re.sub(r"^[•\s]+|\s*\(mới\)$", "", span.get_text(" ", strip=True))
            for span in card.select(".tooltip-content span.text-white")
        ]
        tooltip = [city for city in tooltip if city]
        if tooltip:
            return ", ".join(dict.fromkeys(tooltip))
        loc_span = card.find("span", class_="text-se-neutral-80")
        return loc_span.get_text(strip=True) if loc_span is not None else ""

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        """Parse a ViecLam24h job card (anchor with data-job-id).

        Structure:
        <a data-job-id="200813805" href="/..../id200813805.html?open_from=...">
          <h3 class="text-[16px]...">Job Title</h3>
          <h3 class="text-[14px]...text-[#939295]">Company Name</h3>
          <span class="text-[#2C95FF]">10 - 14 triệu</span>
          <span class="text-se-neutral-80">TP.HCM</span>
          <span class="tooltip-content">... <span class="text-white">• TP.HCM (mới)</span></span>
        </a>
        """
        try:
            href = raw_data.get("href", "")
            source_url = _canonical(href) if href else ""

            h3_tags = raw_data.find_all("h3")
            title = h3_tags[0].get_text(strip=True) if h3_tags else ""
            company = h3_tags[1].get_text(strip=True) if len(h3_tags) > 1 else ""
            if not title:
                return None

            return RawJob(
                title=title,
                company=company,
                location=self._card_location(raw_data),
                salary_raw=self._card_salary(raw_data),
                source=JobSource.VIECLAM24H,
                source_url=source_url,
            )

        except Exception as e:
            logger.warning(f"Failed to parse ViecLam24h job: {e}")
            return None
