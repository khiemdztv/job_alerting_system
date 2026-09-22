"""TopDev current server-rendered job search scraper."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

BASE_URL = "https://topdev.vn"


class TopDevScraper(BaseScraper):
    def __init__(self, *, internship_only: bool = False):
        super().__init__(source=JobSource.TOPDEV)
        self.internship_only = internship_only
        # Retrying a timeout on this large page would consume most of the
        # Telegram webhook budget; the other live sources remain available.
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.session.mount("https://", adapter)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        max_pages = max_pages or self.settings.scrape_max_pages
        jobs: list[RawJob] = []
        for page in range(1, max_pages + 1):
            params: dict[str, str | int] = {"keyword": keyword, "page": page}
            if self.internship_only:
                params["job_levels_ids"] = "1616"
            response = self._get(f"{BASE_URL}/jobs/search", params=params, timeout=8)
            soup = BeautifulSoup(response.text, "html.parser")
            links = soup.find_all("a", href=re.compile(r"/detail-jobs/"))
            if not links:
                break
            for link in links:
                card = link.find_parent("div", class_=re.compile(r"text-card-foreground"))
                if card and (job := self.parse_job(card)):
                    jobs.append(job)
        return jobs

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        title_link = raw_data.find("a", href=re.compile(r"/detail-jobs/"))
        if not title_link:
            return None
        title = title_link.get_text(" ", strip=True)
        company_el = title_link.find_next("span")
        company = company_el.get_text(" ", strip=True) if company_el else ""
        parts = [part.strip() for part in raw_data.stripped_strings if part.strip()]
        location = next(
            (
                part
                for part in parts
                if re.search(
                    r"Hồ Chí Minh|TP\.?\s*HCM|Hà Nội|Đà Nẵng|Bình Dương|Đồng Nai|Remote",
                    part,
                    re.I,
                )
            ),
            "",
        )
        posted = next(
            (
                part
                for part in parts
                if re.fullmatch(r"\d+\s+(?:hours?|days?|weeks?|months?)\s+ago", part, re.I)
            ),
            "",
        )
        description = " ".join(parts)
        if self.internship_only:
            description = f"Intern internship {description}"
        return RawJob(
            title=title,
            company=company,
            location=location,
            salary_raw=next((p for p in parts if "salary" in p.lower()), ""),
            description=description,
            job_type_raw="internship" if self.internship_only else "",
            source=JobSource.TOPDEV,
            source_url=urljoin(BASE_URL, title_link.get("href", "")),
            posted_at_raw=posted,
        )
