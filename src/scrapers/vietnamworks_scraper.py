"""VietnamWorks scraper using the JSON search API the website itself calls.

The search page (``/viec-lam?q=...``) no longer embeds result cards for a
normal browser: its ``__NEXT_DATA__`` carries only ``nbHits`` and the browser
then POSTs to ``ms.vietnamworks.com/job-search/v1.0/search``.  That endpoint
fronts the ``vnw_job_v2`` Algolia index server-side; no Algolia app id or
search key is exposed in page HTML or JS, so the index is not called directly.

The API returns 100 jobs per request with real ``approvedOn`` dates, expiry,
salary range, level, skills and full description, and accepts the same filter
and sort objects the site sends (``workingLocations.cityId``, ``approvedOn``).
The old Next.js path is kept as a batch-only fallback for when the API fails.
"""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.common.text_utils import slugify_vn
from src.scrapers.base_scraper import BLOCKED_STATUSES, BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://www.vietnamworks.com"
API_URL = "https://ms.vietnamworks.com/job-search/v1.0/search"
LAYOUT_CHANGED = "layout_changed"

# workingLocations.cityId, from the page's own ``metaCities`` table.
CITY_IDS: dict[str, int] = {
    "an-giang": 2, "ba-ria-vung-tau": 3, "bac-kan": 4, "bac-giang": 5, "bac-lieu": 6,
    "bac-ninh": 7, "ben-tre": 8, "binh-dinh": 10, "binh-duong": 11, "binh-phuoc": 12,
    "binh-thuan": 13, "ca-mau": 14, "can-tho": 15, "cao-bang": 16, "da-nang": 17,
    "dak-lak": 18, "dong-nai": 19, "dong-thap": 20, "gia-lai": 21, "ha-giang": 22,
    "ha-nam": 23, "ha-noi": 24, "ha-tinh": 26, "hai-duong": 27, "hai-phong": 28,
    "ho-chi-minh": 29, "hoa-binh": 30, "hung-yen": 32, "khanh-hoa": 33, "kon-tum": 34,
    "lai-chau": 35, "lam-dong": 36, "lang-son": 37, "lao-cai": 38, "long-an": 39,
    "nam-dinh": 40, "nghe-an": 41, "ninh-binh": 42, "ninh-thuan": 43, "phu-tho": 44,
    "phu-yen": 45, "quang-binh": 46, "quang-nam": 47, "quang-ngai": 48, "quang-ninh": 49,
    "quang-tri": 50, "soc-trang": 51, "son-la": 52, "tay-ninh": 53, "thai-binh": 54,
    "thai-nguyen": 55, "thanh-hoa": 56, "thua-thien-hue": 57, "tien-giang": 58,
    "tra-vinh": 59, "tuyen-quang": 60, "kien-giang": 61, "vinh-long": 62, "vinh-phuc": 63,
    "yen-bai": 65, "dien-bien": 69, "quoc-te": 70, "hau-giang": 72, "dak-nong": 73,
}
_CITY_ALIASES = {
    "hcm": "ho-chi-minh", "tphcm": "ho-chi-minh", "sai-gon": "ho-chi-minh",
    "saigon": "ho-chi-minh", "hn": "ha-noi", "hue": "thua-thien-hue",
}

# typeWorkingId, from the site's filter list.
TYPE_WORKING = {
    1: "Toàn thời gian", 2: "Bán thời gian", 3: "Thực tập", 4: "Nghề tự do (freelance)",
    5: "Hợp đồng thời vụ", 6: "Khác", 7: "Việc làm online",
}

# Fields the website itself asks for; the API currently returns more anyway.
_RETRIEVE_FIELDS = [
    "jobId", "jobTitle", "jobUrl", "alias", "companyName", "workingLocations", "address",
    "prettySalary", "salaryMin", "salaryMax", "salaryCurrency", "isSalaryVisible",
    "approvedOn", "createdOn", "expiredOn", "jobLevelVI", "typeWorkingId", "skills",
    "industriesV3", "jobDescription", "jobRequirement", "yearsOfExperience",
]
_TEXT_LIMIT = 1500


def city_id(location: str | None) -> int | None:
    """Map "TP. Hồ Chí Minh", "HCM", "Hà Nội" ... to a VietnamWorks cityId."""
    slug = slugify_vn(location or "")
    for prefix in ("thanh-pho-", "tp-", "tinh-"):
        slug = slug.removeprefix(prefix)
    return CITY_IDS.get(_CITY_ALIASES.get(slug, slug))


def _html_text(value: object) -> str:
    text = str(value or "")
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())[:_TEXT_LIMIT]


def _canonical(url: str) -> str:
    parts = urlsplit(urljoin(f"{BASE_URL}/", url.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class VietnamWorksScraper(BaseScraper):
    SEARCH_URL = f"{BASE_URL}/viec-lam"
    API_URL = API_URL
    PAGE_SIZE = 100
    LIVE_PAGE_SIZE = 50

    def __init__(self):
        super().__init__(source=JobSource.VIETNAMWORKS)

    # ── public entry points ─────────────────────────────────────

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: str | None = None,
        newest: bool | None = None,
    ) -> list[RawJob]:
        """Search by keyword, optionally limited to one city.

        ``newest`` defaults to True for batch runs (fresh jobs first) and to
        False in live mode, where only one page is read and the site's own
        relevance order returns more on-topic titles.
        """
        if newest is None:
            newest = not self.live_mode
        return self._search(keyword, max_pages, location, newest)

    def scrape_latest(
        self, max_pages: Optional[int] = None, location: str | None = None
    ) -> list[RawJob]:
        """Every active job, newest first, without a keyword."""
        return self._search("", max_pages, location, True)

    # ── API path ────────────────────────────────────────────────

    def _search(
        self, keyword: str, max_pages: Optional[int], location: str | None, newest: bool
    ) -> list[RawJob]:
        max_pages = 1 if self.live_mode else (max_pages or self.settings.scrape_max_pages)
        jobs: list[RawJob] = []
        try:
            for page in range(max_pages):
                body = self._fetch_api_page(keyword, page, location, newest)
                hits = body["data"]
                parsed = [job for hit in hits if (job := self.parse_job(hit))]
                if hits and not parsed:
                    self.last_error = LAYOUT_CHANGED
                    break
                jobs.extend(parsed)
                meta = body.get("meta") or {}
                if page == 0 and not hits and int(meta.get("nbHits") or 0) > 0:
                    self.last_error = LAYOUT_CHANGED
                if not hits or page + 1 >= int(meta.get("nbPages") or 0):
                    break
        except requests.RequestException as exc:
            logger.warning("VietnamWorks API failed: %s", exc)
            if not jobs and self._may_fall_back(exc):
                return self._scrape_next_data(keyword, max_pages)
        except (ValueError, KeyError, TypeError) as exc:  # JSON shape changed
            logger.warning("VietnamWorks API returned an unexpected body: %s", exc)
            self.last_error = LAYOUT_CHANGED
            if not jobs and not self.live_mode:
                return self._scrape_next_data(keyword, max_pages)
        except TimeoutError:
            pass  # scrape budget exhausted; keep what we have
        return jobs

    def _fetch_api_page(
        self, keyword: str, page: int, location: str | None, newest: bool = True
    ) -> dict:
        filters = []
        wanted_city = city_id(location)
        if wanted_city is not None:
            filters.append({"field": "workingLocations.cityId", "value": str(wanted_city)})
        elif location:
            logger.info("VietnamWorks has no city id for %r; searching all cities", location)
        payload = {
            "userId": 0,
            "query": keyword.strip(),
            "filter": filters,
            "ranges": [],
            "order": [{"field": "approvedOn", "value": "desc"}] if newest else [],
            "hitsPerPage": self.LIVE_PAGE_SIZE if self.live_mode else self.PAGE_SIZE,
            "page": page,
            "retrieveFields": _RETRIEVE_FIELDS,
        }
        response = self._post(
            self.API_URL,
            json_data=payload,
            headers={
                "Accept": "application/json",
                "Accept-Language": "vi",
                "X-Source": "Page-Container",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/",
            },
        )
        body = response.json()
        meta = body.get("meta") or {}
        if meta.get("code") not in (200, "200") or not isinstance(body.get("data"), list):
            raise ValueError(f"unexpected meta {meta!r}")
        return body

    def _may_fall_back(self, exc: requests.RequestException) -> bool:
        """The Next.js page costs a second request; never in live mode or when blocked."""
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return not self.live_mode and status not in BLOCKED_STATUSES

    # ── Next.js fallback ────────────────────────────────────────

    def _scrape_next_data(self, keyword: str, max_pages: int) -> list[RawJob]:
        """Read hits embedded in the search page, when the server renders them."""
        jobs: list[RawJob] = []
        try:
            for page in range(1, max_pages + 1):
                response = self._get(self.SEARCH_URL, params={"q": keyword, "page": page})
                search = self._next_search_data(response.text)
                if search is None:
                    self.last_error = LAYOUT_CHANGED
                    break
                result = search.get("searchResultData") or {}
                hits = result.get("hits") or []
                if not hits:
                    # Results are rendered client-side for normal browsers.
                    if page == 1 and int(result.get("nbHits") or 0) > 0:
                        self.last_error = self.last_error or LAYOUT_CHANGED
                    break
                jobs.extend(job for hit in hits if (job := self.parse_job(hit)))
        except (requests.RequestException, TimeoutError) as exc:
            logger.warning("VietnamWorks Next.js fallback failed: %s", exc)
        return jobs

    @staticmethod
    def _next_search_data(html: str) -> dict | None:
        script = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return None
        try:
            body = json.loads(script.string)
        except ValueError:
            return None
        search = body.get("props", {}).get("pageProps", {}).get("seoSearch")
        return search if isinstance(search, dict) else None

    # ── parsing ─────────────────────────────────────────────────

    def parse_job(self, raw_data: dict) -> Optional[RawJob]:
        if not isinstance(raw_data, dict):
            return None
        title = str(raw_data.get("jobTitle") or "").strip()
        url = str(raw_data.get("jobUrl") or "").strip()
        if not url and raw_data.get("alias") and raw_data.get("jobId"):
            url = f"{BASE_URL}/{raw_data['alias']}-{raw_data['jobId']}-jv"
        if not title or not url:
            return None

        cities = []
        for item in raw_data.get("workingLocations") or []:
            if isinstance(item, dict):
                name = item.get("cityNameVI") or item.get("cityName") or item.get("address")
                if name:
                    cities.append(str(name).strip())
        location = ", ".join(dict.fromkeys(cities))

        skills = ", ".join(
            str(item.get("skillName") or "").strip()
            for item in (raw_data.get("skills") or [])
            if isinstance(item, dict) and item.get("skillName")
        )
        description = _html_text(raw_data.get("jobDescription"))
        if skills:
            description = f"{description}\nKỹ năng: {skills}".strip()

        type_id = raw_data.get("typeWorkingId")
        job_type = TYPE_WORKING.get(type_id, "") if isinstance(type_id, int) else ""
        return RawJob(
            title=title,
            company=str(raw_data.get("companyName") or ""),
            location=location,
            salary_raw=str(raw_data.get("prettySalary") or ""),
            description=description,
            requirements=_html_text(raw_data.get("jobRequirement")),
            job_type_raw=job_type,
            source=JobSource.VIETNAMWORKS,
            source_url=_canonical(url),
            posted_at_raw=str(raw_data.get("approvedOn") or raw_data.get("createdOn") or ""),
            valid_through_raw=str(raw_data.get("expiredOn") or ""),
        )
