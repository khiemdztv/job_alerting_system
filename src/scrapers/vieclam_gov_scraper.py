"""vieclam.gov.vn (Vietnam national job exchange) scraper.

The portal is a Vite/React SPA, so the HTML shell is empty.  Its public search
page calls an unauthenticated JSON endpoint, which is what we use:

    POST https://api.vieclam.gov.vn/svl-admin/webhook/api/svl/nld/vieclam
         ?page_num=<1-based>&page_size=<n>      body: {"tencv": "<keyword>"}

Response: ``{"success": true, "data": [job, ...], "pagination": {total_items,
total_pages, currentPage, pageSize}}``.  An empty ``tencv`` returns every active
job, newest approved first.  robots.txt allows ``/search/`` and sets
``Crawl-delay: 1`` (honoured by the base-class rate limiter).  Job detail page:
``/search/job-detail?id=<id>``.  ``location`` is resolved to a province id via
``GET /api/svl/tinh_thanhpho/list`` and sent as ``"noi_lam_viec": [id]``; when no
province matches it falls back to a post-filter on ``ten_tinh1`` / ``ten_tinh2``.
"""

from __future__ import annotations

from typing import Any, Optional

from src.common.models import JobSource, RawJob
from src.common.text_utils import slugify_vn
from src.scrapers.base_scraper import BaseScraper

SITE_URL = "https://vieclam.gov.vn"
API_BASE = "https://api.vieclam.gov.vn/svl-admin/webhook/api/svl"
API_URL = f"{API_BASE}/nld/vieclam"
PROVINCES_URL = f"{API_BASE}/tinh_thanhpho/list"
PAGE_SIZE = 20
_PROVINCES: dict[str, int] | None = None

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": SITE_URL,
    "Referer": f"{SITE_URL}/",
}


class VieclamGovScraper(BaseScraper):
    def __init__(self):
        super().__init__(source=JobSource.VIECLAM_GOV)

    # -- public API -------------------------------------------------------
    def scrape(
        self, keyword: str, max_pages: Optional[int] = None, location: Optional[str] = None
    ) -> list[RawJob]:
        return self._scrape(keyword, max_pages, location)

    def scrape_latest(
        self, max_pages: Optional[int] = None, location: Optional[str] = None
    ) -> list[RawJob]:
        return self._scrape("", max_pages, location)

    # -- internals --------------------------------------------------------
    def _scrape(
        self, keyword: str, max_pages: Optional[int], location: Optional[str]
    ) -> list[RawJob]:
        max_pages = 1 if self.live_mode else (max_pages or self.settings.scrape_max_pages)
        self.last_error = ""
        city = slugify_vn(location or "")
        body: dict[str, Any] = {"tencv": keyword.strip()}
        province_id = self._province_id(city) if city else None
        if province_id is not None:
            body["noi_lam_viec"] = [province_id]
            city = ""  # filtered server side
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            response = self._post(
                API_URL,
                json_data=body,
                params={"page_num": page, "page_size": PAGE_SIZE},
                headers=_HEADERS,
            )
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                if page == 1:
                    self.last_error = "layout_changed"
                break
            if not rows:
                break
            for row in rows:
                job = self.parse_job(row)
                if not job or job.source_url in seen:
                    continue
                if city and city not in slugify_vn(job.location):
                    continue
                seen.add(job.source_url)
                jobs.append(job)
            pagination = payload.get("pagination") or {}
            if page >= int(pagination.get("total_pages") or page):
                break
        return jobs

    def _province_id(self, city_slug: str) -> Optional[int]:
        """Resolve a city name to the portal's province id (None if unknown)."""
        global _PROVINCES
        if _PROVINCES is None:
            try:
                rows = self._get(PROVINCES_URL, headers=_HEADERS).json().get("data") or []
                _PROVINCES = {
                    slugify_vn(str(r.get("ten", ""))): int(r["id"])
                    for r in rows
                    if str(r.get("id", "")).isdigit()
                }
            except Exception:  # lookup is best-effort; post-filter still works
                self.last_error = ""
                return None
        for name, pid in _PROVINCES.items():
            if city_slug and (city_slug in name):
                return pid
        return None

    def parse_job(self, raw_data: dict[str, Any]) -> Optional[RawJob]:
        title = str(raw_data.get("vitri_td") or "").strip()
        job_id = raw_data.get("id")
        if not title or job_id is None:
            return None
        provinces = [
            str(raw_data[k]).strip() for k in ("ten_tinh1", "ten_tinh2") if raw_data.get(k)
        ]
        salary = str(raw_data.get("muc_luong") or "").strip()

        facts = [
            ("Ngành nghề", raw_data.get("nganh_nghe")),
            ("Chức vụ", raw_data.get("chuc_vu")),
            ("Số lượng", raw_data.get("so_luong")),
            ("Quy mô", raw_data.get("quymo")),
            ("Hạn nộp", raw_data.get("ngay_het_han")),
        ]
        description = "; ".join(f"{label}: {value}" for label, value in facts if value)

        posted = str(raw_data.get("ngay_duyet") or "").strip()
        if posted:
            posted = posted[:19]  # "2026-10-30 08:25:28" (drop fraction + tz)

        return RawJob(
            title=title,
            company=str(raw_data.get("ten_ct") or "").strip(),
            location=", ".join(provinces),
            salary_raw=salary,
            description=description,
            requirements=str(raw_data.get("kinh_nghiem_lv") or "").strip(),
            job_type_raw=str(raw_data.get("hinhthuc_lv") or "").strip(),
            source=JobSource.VIECLAM_GOV,
            source_url=f"{SITE_URL}/search/job-detail?id={job_id}",
            posted_at_raw=posted,
        )
