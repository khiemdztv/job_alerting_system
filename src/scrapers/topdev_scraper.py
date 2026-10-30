"""TopDev scraper backed by the public JSON API its own front end calls.

``https://topdev.vn/jobs/search`` is a heavy server-rendered page that often
takes >8 s or drops the TLS connection (the ReadTimeout seen in live search).
The site's job list is fed by ``https://api.topdev.vn/td/v2/jobs`` — public,
no key, JSON, behind Cloudflare's CDN — which answers in ~1.5–2.5 s (occasionally
up to ~6 s) and carries structured fields the HTML card lacked.

Verified (30 Oct 2026):
- ``keyword``          free text (UTF-8, diacritics fine)
- ``page``/``page_size`` 1-based pagination, up to 50 per page; ``meta.last_page``
- ``job_levels_ids=1616`` internship level
- ``region_ids``       province code ("01" Hà Nội, "79" Hồ Chí Minh)
- ``fields[job]``/``fields[company]`` sparse field selection (keeps the payload small)
- Without ``keyword`` the list is ordered by most recently refreshed — used by
  ``scrape_latest``.  Keyword results are relevance-ordered (no newest sort found).
- Salaries are masked ("1****** - 2****** VND") unless logged in, so only an
  unmasked value or "negotiable" is kept.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import requests

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.common.text_utils import slugify_vn
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://topdev.vn"
API_URL = "https://api.topdev.vn/td/v2/jobs"
INTERNSHIP_LEVEL_ID = "1616"
PAGE_SIZE = 50

_JOB_FIELDS = (
    "id,title,slug,salary,company,skills_str,job_types_str,job_levels_str,"
    "addresses,detail_url,published,refreshed"
)
_COMPANY_FIELDS = "display_name"

# Province codes TopDev uses in region_ids / address_region_ids.
REGION_IDS: dict[str, str] = {
    "ha-noi": "01",
    "hai-phong": "31",
    "da-nang": "48",
    "ho-chi-minh": "79",
    "can-tho": "92",
}
_CITY_ALIASES = {
    "hcm": "ho-chi-minh", "tphcm": "ho-chi-minh", "sai-gon": "ho-chi-minh",
    "saigon": "ho-chi-minh", "hn": "ha-noi",
}
_REGION_PREFIX = re.compile(r"^(?:Thành phố|Tỉnh)\s+", re.I)


def city_slug(location: str | None) -> str:
    slug = slugify_vn(location or "")
    slug = re.sub(r"^(?:thanh-pho|tp|tinh)-", "", slug)
    return _CITY_ALIASES.get(slug, slug)


def _iso_date(value: str) -> str:
    """'30-10-2026' -> '2026-10-30T00:00:00+07:00' (TopDev dates are Vietnam time)."""
    try:
        return datetime.strptime(value.strip(), "%d-%m-%Y").strftime("%Y-%m-%dT00:00:00+07:00")
    except (ValueError, AttributeError):
        return ""


class TopDevScraper(BaseScraper):
    def __init__(self, *, internship_only: bool = False):
        super().__init__(source=JobSource.TOPDEV)
        self.internship_only = internship_only
        # A retried timeout would consume most of the Telegram webhook budget.
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.session.mount("https://", adapter)

    def _params(self, location: str | None) -> dict[str, str | int]:
        params: dict[str, str | int] = {
            "locale": "vi_VN",
            "page_size": PAGE_SIZE,
            "fields[job]": _JOB_FIELDS,
            "fields[company]": _COMPANY_FIELDS,
        }
        if self.internship_only:
            params["job_levels_ids"] = INTERNSHIP_LEVEL_ID
        region = REGION_IDS.get(city_slug(location)) if location else None
        if region:
            params["region_ids"] = region
        return params

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: str | None = None,
    ) -> list[RawJob]:
        params = self._params(location)
        params["keyword"] = keyword.strip()
        return self._scrape_api(params, max_pages)

    def scrape_latest(
        self, max_pages: Optional[int] = None, location: str | None = None
    ) -> list[RawJob]:
        """Most recently refreshed postings, no keyword."""
        return self._scrape_api(self._params(location), max_pages)

    def _scrape_api(self, params: dict, max_pages: Optional[int]) -> list[RawJob]:
        max_pages = 1 if self.live_mode else (max_pages or self.settings.scrape_max_pages)
        jobs: list[RawJob] = []
        for page in range(1, max_pages + 1):
            try:
                response = self._get(
                    API_URL,
                    params=dict(params, page=page),
                    headers={"Accept": "application/json", "Referer": f"{BASE_URL}/"},
                    timeout=8,
                )
                body = response.json()
            except (requests.RequestException, TimeoutError) as exc:
                logger.warning(f"TopDev page {page} failed: {exc}")
                break
            except ValueError:
                self.mark_layout_changed()
                logger.warning("TopDev API answered with non-JSON content")
                break

            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list):
                self.mark_layout_changed()
                logger.warning("TopDev API response has no 'data' list")
                break
            parsed = [job for item in data if (job := self.parse_job(item))]
            if data and not parsed and page == 1:
                self.mark_layout_changed()
            jobs.extend(parsed)

            meta = body.get("meta") or {}
            if not data or page >= int(meta.get("last_page") or page):
                break
        return jobs

    def parse_job(self, raw_data: dict) -> Optional[RawJob]:
        if not isinstance(raw_data, dict):
            return None
        title = str(raw_data.get("title") or "").strip()
        url = str(raw_data.get("detail_url") or "").split("?")[0]
        if not url and raw_data.get("slug") and raw_data.get("id"):
            url = f"{BASE_URL}/viec-lam/{raw_data['slug']}-{raw_data['id']}"
        if not title or not url:
            return None

        company = raw_data.get("company") or {}
        addresses = raw_data.get("addresses") or {}
        regions = addresses.get("address_region_array") or []
        location = ", ".join(_REGION_PREFIX.sub("", str(r)).strip() for r in regions if r)
        if not location:
            location = str(addresses.get("sort_addresses") or "").strip()

        salary = raw_data.get("salary") or {}
        salary_raw = str(salary.get("value") or "").strip()
        if "*" in salary_raw:  # masked for anonymous visitors
            salary_raw = ""
        if not salary_raw and str(salary.get("is_negotiable")) == "1":
            salary_raw = "Thương lượng"

        levels = str(raw_data.get("job_levels_str") or "")
        types = str(raw_data.get("job_types_str") or "")
        skills = str(raw_data.get("skills_str") or "")
        job_type_raw = ", ".join(p for p in (types, levels) if p)
        if self.internship_only:
            job_type_raw = f"internship, {job_type_raw}" if job_type_raw else "internship"

        description = ". ".join(
            p for p in (
                f"Kỹ năng: {skills}" if skills else "",
                f"Cấp bậc: {levels}" if levels else "",
                f"Hình thức: {types}" if types else "",
            ) if p
        )
        if self.internship_only:
            description = f"Intern internship {description}".strip()

        published = (raw_data.get("published") or {}).get("date") or ""
        refreshed = (raw_data.get("refreshed") or {}).get("date") or ""

        return RawJob(
            title=title,
            company=str(company.get("display_name") or "") if isinstance(company, dict) else "",
            location=location,
            salary_raw=salary_raw,
            description=description,
            job_type_raw=job_type_raw,
            source=JobSource.TOPDEV,
            source_url=url,
            posted_at_raw=_iso_date(published) or _iso_date(refreshed),
        )
