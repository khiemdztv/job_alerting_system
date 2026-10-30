"""TopCV jobs found through the You.com search index.

TopCV answers every non-browser request with HTTP 403, including the sitemap
its robots.txt advertises, so its pages are not read directly.  A search index
that already crawled TopCV gives title, link and snippet for recent postings.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

TOPCV_DOMAINS = ["topcv.vn"]

_TITLE_SUFFIX = re.compile(r"\s*[|\-–]\s*(?:Tuyển dụng\s*)?TopCV(?:\.vn)?\s*$", re.IGNORECASE)
_SALARY = re.compile(
    r"(?:Thu nhập|Mức lương|Lương)\s*:?\s*"
    r"(\d[\d.,]*(?:\s*(?:-|–|đến)\s*\d[\d.,]*)?\s*(?:triệu|trb|vn[đd]|usd)"
    r"|thỏa thuận|thương lượng)",
    re.IGNORECASE,
)


class TopCVScraper(BaseScraper):
    def __init__(self, client=None):
        super().__init__(source=JobSource.TOPCV)
        if client is None:
            # Imported here: src.web_search imports the registry, which imports us.
            from src.web_search.you_search import YouSearchClient

            client = YouSearchClient(self.settings)
        self.client = client

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        # One search per keyword: the index returns up to 100 results at once,
        # and each extra request spends You.com credit.
        results = self.client.search(
            keyword,
            limit=self.settings.you_search_count,
            deadline_at=self.deadline_at,
            domains=TOPCV_DOMAINS,
        )
        return [job for result in results if (job := self.parse_job(result))]

    def parse_job(self, raw_data: dict) -> Optional[RawJob]:
        url = str(raw_data.get("source_url") or "").strip()
        title = _TITLE_SUFFIX.sub("", str(raw_data.get("title") or "")).strip()
        # Only individual postings; category and company pages are not jobs.
        if not title or "topcv.vn/viec-lam/" not in url:
            return None
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))  # drop tracking query
        description = str(raw_data.get("description") or "")
        salary = str(raw_data.get("salary") or "")
        if not salary:
            match = _SALARY.search(f"{title} {description}")
            salary = match.group(1).strip() if match else ""
        return RawJob(
            title=title,
            company=str(raw_data.get("company") or ""),
            location=str(raw_data.get("location") or ""),
            salary_raw=salary,
            description=description,
            source=JobSource.TOPCV,
            source_url=url,
            posted_at_raw=str(raw_data.get("posted_at") or ""),
        )
