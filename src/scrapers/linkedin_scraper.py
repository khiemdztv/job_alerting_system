"""LinkedIn jobs through the public guest search endpoint (no login).

Same endpoint `python-jobspy` uses: `/jobs-guest/jobs/api/seeMoreJobPostings/search`
returns an HTML fragment of 10 job cards per page.  Cards carry title, company,
location and posting date only; there is no salary or description without a
second request per job, which this scraper deliberately does not make.

LinkedIn's terms forbid scraping and it drops connections after a handful of
requests from one IP (observed after 2 requests), so keep this a low-frequency
source: one page per search, never in the interactive burst of every board
unless the caller accepts the miss.
"""

from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}"
PAGE_SIZE = 10
MAX_START = 1000
MAX_PAGES_DEFAULT = 1
DEFAULT_LOCATION = "Vietnam"
HOURS_OLD = 72

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}

REMOTE_WORDS = ("remote", "work from home", "wfh", "từ xa")


class LinkedInScraper(BaseScraper):
    def __init__(self, hours_old: int = HOURS_OLD):
        super().__init__(source=JobSource.LINKEDIN)
        self.hours_old = hours_old

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: Optional[str] = None,
    ) -> list[RawJob]:
        pages = 1 if self.live_mode else (max_pages or MAX_PAGES_DEFAULT)
        params = {
            "keywords": keyword,
            "location": location or DEFAULT_LOCATION,
            "f_TPR": f"r{int(self.hours_old) * 3600}",
        }
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for page in range(pages):
            start = page * PAGE_SIZE
            if start >= MAX_START:
                break
            query = {**params, "start": start}
            response = self._get(SEARCH_URL, params=query, headers=dict(HEADERS))
            cards = BeautifulSoup(response.text, "html.parser").find_all(class_="base-search-card")
            if not cards:
                break
            for card in cards:
                job = self.parse_job(card)
                if job and job.source_url not in seen:
                    seen.add(job.source_url)
                    jobs.append(job)
            if len(cards) < PAGE_SIZE:
                break
        return jobs

    def parse_job(self, raw_data) -> Optional[RawJob]:
        card = raw_data
        job_id = str(card.get("data-entity-urn", "")).split(":")[-1]
        title_tag = card.find("h3", class_="base-search-card__title")
        if not job_id.isdigit() or title_tag is None:
            return None
        title = _clean(title_tag.get_text(" "))
        if not title:
            return None
        company_tag = card.find("h4", class_="base-search-card__subtitle")
        location_tag = card.find("span", class_="job-search-card__location")
        time_tag = card.find("time")
        location = _clean(location_tag.get_text(" ")) if location_tag else ""
        job_type = "remote" if any(w in f"{title} {location}".lower() for w in REMOTE_WORDS) else ""
        return RawJob(
            title=title,
            company=_clean(company_tag.get_text(" ")) if company_tag else "",
            location=location,
            job_type_raw=job_type,
            source=JobSource.LINKEDIN,
            source_url=JOB_URL.format(job_id=job_id),
            posted_at_raw=str(time_tag.get("datetime", "")) if time_tag else "",
        )


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
