"""JobOKO (vn.joboko.com) scraper over server-rendered SEO listing pages.

Verified markup (30 Oct 2026):
- Keyword listing: ``/tim-viec-lam-<slug>`` (``?p=<n>``, 20 cards/page).  Only
  slugs JobOKO already indexes exist (``nhan-vien-ke-toan`` yes, ``ke-toan``
  404), so a 404 is retried once as ``nhan-vien-<slug>``.  Location variants are
  ``/tim-viec-lam-<slug>-tai-<city>`` (major cities only; others 404 and fall
  back to a post-filter).  The query endpoints ``/jobs?*`` and ``/s/*`` are
  disallowed by robots.txt and are never used.
- Rich card: ``div.item[data-jid]`` with ``h2.item-title a``, ``.item-company``,
  ``.item-address``, ``.item-rate`` (salary), ``.item-text`` bullets and
  ``.item-date[data-value]`` which is the application DEADLINE, not the post date.
- Newest: ``/viec-lam-moi`` uses a compact card (``div.item`` + ``h3.item-ttl``,
  ``p.item-txt``, ``.item-price``).
- Detail pages carry schema.org JobPosting JSON-LD; no sitemap is published.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from src.common.models import JobSource, RawJob
from src.common.text_utils import slugify_vn
from src.scrapers.base_scraper import BaseScraper

BASE_URL = "https://vn.joboko.com"
_JOB_HREF = re.compile(r"-xvi\d+/?$")


def _text(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def _is_not_found(exc: requests.RequestException) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


class JobokoScraper(BaseScraper):
    def __init__(self):
        super().__init__(source=JobSource.JOBOKO)

    # -- public API -------------------------------------------------------
    def scrape(
        self, keyword: str, max_pages: Optional[int] = None, location: Optional[str] = None
    ) -> list[RawJob]:
        slug = slugify_vn(keyword)
        if not slug:
            return self.scrape_latest(max_pages, location)
        self.last_error = ""
        city = slugify_vn(location or "")
        paths = []
        if city:
            paths.append(f"/tim-viec-lam-{slug}-tai-{city}")
        paths.append(f"/tim-viec-lam-{slug}")
        if not slug.startswith("nhan-vien-"):
            paths.append(f"/tim-viec-lam-nhan-vien-{slug}")
        return self._crawl(paths, max_pages, location)

    def scrape_latest(
        self, max_pages: Optional[int] = None, location: Optional[str] = None
    ) -> list[RawJob]:
        """Newest postings from ``/viec-lam-moi`` (descending job id)."""
        self.last_error = ""
        return self._crawl(["/viec-lam-moi"], max_pages, location)

    # -- internals --------------------------------------------------------
    def _crawl(
        self, candidates: list[str], max_pages: Optional[int], location: Optional[str]
    ) -> list[RawJob]:
        max_pages = 1 if self.live_mode else (max_pages or self.settings.scrape_max_pages)
        city = slugify_vn(location or "")
        jobs: list[RawJob] = []
        seen: set[str] = set()

        path, html = None, ""
        for candidate in candidates:
            try:
                html = self._get(BASE_URL + candidate).text
            except requests.RequestException as exc:
                if _is_not_found(exc):
                    self.last_error = ""
                    continue
                raise
            path = candidate
            break
        if path is None:
            self.last_error = "no_listing_for_keyword"
            return []

        # A location variant that exists is already filtered server side.
        server_filtered = bool(city) and f"-tai-{city}" in path
        for page in range(1, max_pages + 1):
            if page > 1:
                html = self._get(BASE_URL + path, params={"p": page}).text
            cards = self._cards(BeautifulSoup(html, "html.parser"))
            if not cards:
                if page == 1:
                    self.last_error = "layout_changed"
                break
            for card in cards:
                job = self.parse_job(card)
                if not job or job.source_url in seen:
                    continue
                if city and not server_filtered and city not in slugify_vn(job.location):
                    continue
                seen.add(job.source_url)
                jobs.append(job)
        return jobs

    @staticmethod
    def _cards(soup: BeautifulSoup) -> list[Tag]:
        return [
            div
            for div in soup.select("div.item")
            if div.select_one("h2.item-title a[href], h3.item-ttl a[href]")
        ]

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        link = raw_data.select_one("h2.item-title a[href], h3.item-ttl a[href]")
        if not link or not _JOB_HREF.search(link["href"].strip()):
            return None
        title = _text(link)
        company = _text(raw_data.select_one(".item-company, p.item-txt"))
        location = _text(raw_data.select_one(".item-address"))
        salary = _text(raw_data.select_one(".item-rate, .item-price"))

        bullets = [_text(li) for li in raw_data.select(".item-text li")]
        description = "; ".join(b for b in bullets if b)
        date_el = raw_data.select_one(".item-date")
        deadline = _text(date_el)
        if deadline:
            description = f"{description} | Hạn nộp hồ sơ: {deadline}".strip(" |")

        return RawJob(
            title=title,
            company=company,
            location=location,
            salary_raw=salary,
            description=description,
            source=JobSource.JOBOKO,
            source_url=urljoin(BASE_URL, link["href"].strip()),
            posted_at_raw="",
        )
