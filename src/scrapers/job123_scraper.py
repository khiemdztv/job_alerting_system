"""123job.vn scraper (server-rendered Laravel/Livewire listing pages).

Verified markup (30 Oct 2026), ``GET /tuyen-dung?q=<kw>&page=<n>``:
- 30 cards per page, newest first when ``q`` is empty (``/tuyen-dung``).
- Title: ``a[href=/viec-lam/<slug>-<10 chars>]`` (the only text-bearing job link).
- Company: ``p.uppercase`` in the same row.
- Salary: ``span.text-orange-500``; location: the span after the "·" separator.
- Posted: text such as ``Đăng: 2 giờ trước``.
robots.txt only disallows ``/ajax`` and ``/apply``; its sitemap index is empty.
The site ignores city query parameters, so ``location`` is applied as a
post-filter on the parsed card location.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup, Tag

from src.common.models import JobSource, RawJob
from src.common.text_utils import slugify_vn
from src.scrapers.base_scraper import BaseScraper

BASE_URL = "https://123job.vn"

_JOB_HREF = re.compile(r"/viec-lam/[^/?#]+-[A-Za-z0-9]{10}/?$")
_POSTED = re.compile(r"Đăng:\s*(.+)", re.I)


def _text(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


class Job123Scraper(BaseScraper):
    def __init__(self):
        super().__init__(source=JobSource.JOB123)

    # -- public API -------------------------------------------------------
    def scrape(
        self, keyword: str, max_pages: Optional[int] = None, location: Optional[str] = None
    ) -> list[RawJob]:
        return self._scrape(keyword, max_pages, location)

    def scrape_latest(
        self, max_pages: Optional[int] = None, location: Optional[str] = None
    ) -> list[RawJob]:
        """Newest jobs, no keyword (``/tuyen-dung`` is ordered by post time)."""
        return self._scrape("", max_pages, location)

    # -- internals --------------------------------------------------------
    def _scrape(
        self, keyword: str, max_pages: Optional[int], location: Optional[str]
    ) -> list[RawJob]:
        max_pages = 1 if self.live_mode else (max_pages or self.settings.scrape_max_pages)
        self.last_error = ""
        jobs: list[RawJob] = []
        seen: set[str] = set()
        wanted = slugify_vn(location or "")
        for page in range(1, max_pages + 1):
            params: dict[str, object] = {}
            if keyword.strip():
                params["q"] = keyword.strip()
            if page > 1:
                params["page"] = page
            response = self._get(f"{BASE_URL}/tuyen-dung", params=params or None)
            cards = self._cards(BeautifulSoup(response.text, "html.parser"))
            if not cards:
                if page == 1:
                    self.last_error = "layout_changed"
                break
            for card in cards:
                job = self.parse_job(card)
                if not job or job.source_url in seen:
                    continue
                if wanted and wanted not in slugify_vn(job.location):
                    continue
                seen.add(job.source_url)
                jobs.append(job)
        return jobs

    @staticmethod
    def _title_link(node: Tag) -> Tag | None:
        for a in node.find_all("a", href=True):
            if _JOB_HREF.search(a["href"]) and a.get_text(strip=True):
                return a
        return None

    def _cards(self, soup: BeautifulSoup) -> list[Tag]:
        """One element per job: the smallest ancestor holding exactly one title link."""
        cards: list[Tag] = []
        for a in soup.find_all("a", href=True):
            if not (_JOB_HREF.search(a["href"]) and a.get_text(strip=True)):
                continue
            node: Tag = a
            while node.parent is not None and isinstance(node.parent, Tag):
                titles = {
                    x["href"]
                    for x in node.parent.find_all("a", href=True)
                    if _JOB_HREF.search(x["href"]) and x.get_text(strip=True)
                }
                if len(titles) > 1 or node.parent.name in ("body", "html"):
                    break
                if node.parent.find(attrs={"data-job-id": True}) is not None:
                    node = node.parent
                    break
                node = node.parent
            cards.append(node)
        return cards

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        link = self._title_link(raw_data)
        if not link:
            return None
        title = link.get_text(" ", strip=True)
        company = _text(raw_data.select_one("p.uppercase"))
        salary_el = raw_data.select_one("span.text-orange-500")
        salary = _text(salary_el)
        if not salary.strip("_ "):  # "__" placeholder means "not shown"
            salary = ""

        location = ""
        if salary_el is not None:
            row = salary_el.find_parent("div")
            spans = [
                s
                for s in (row.find_all("span", recursive=False) if row else [])
                if s.get_text(strip=True) not in ("", "·") and s.find("span") is None
            ]
            location = _text(spans[0]) if spans else ""
        else:
            row = raw_data.select_one("div.flex-wrap")
            if row:
                parts = [
                    _text(s)
                    for s in row.find_all("span", recursive=False)
                    if _text(s) not in ("", "·")
                ]
                location = parts[0] if parts else ""

        posted = ""
        for node in raw_data.find_all(string=_POSTED):
            m = _POSTED.search(node)
            if m:
                posted = m.group(1).strip()
                break

        return RawJob(
            title=title,
            company=company,
            location=location,
            salary_raw=salary,
            description="",
            source=JobSource.JOB123,
            source_url=urljoin(BASE_URL, link["href"]),
            posted_at_raw=posted,
        )


def search_url(keyword: str, page: int = 1) -> str:
    """Public search URL (documentation helper)."""
    url = f"{BASE_URL}/tuyen-dung?q={quote_plus(keyword)}"
    return url if page <= 1 else f"{url}&page={page}"
