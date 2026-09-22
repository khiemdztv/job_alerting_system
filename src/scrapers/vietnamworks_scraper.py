"""VietnamWorks current search scraper using its server-rendered Next.js data."""

from __future__ import annotations

import json
from typing import Optional

from bs4 import BeautifulSoup

from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper


class VietnamWorksScraper(BaseScraper):
    SEARCH_URL = "https://www.vietnamworks.com/viec-lam"

    def __init__(self):
        super().__init__(source=JobSource.VIETNAMWORKS)
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        )

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        max_pages = max_pages or self.settings.scrape_max_pages
        jobs: list[RawJob] = []
        for page in range(1, max_pages + 1):
            response = self._get(self.SEARCH_URL, params={"q": keyword, "page": page})
            script = BeautifulSoup(response.text, "html.parser").find("script", id="__NEXT_DATA__")
            if not script or not script.string:
                break
            body = json.loads(script.string)
            hits = (
                body.get("props", {})
                .get("pageProps", {})
                .get("seoSearch", {})
                .get("searchResultData", {})
                .get("hits", [])
            )
            if not hits:
                break
            jobs.extend(job for hit in hits if (job := self.parse_job(hit)))
        return jobs

    def parse_job(self, raw_data: dict) -> Optional[RawJob]:
        title = str(raw_data.get("jobTitle") or "").strip()
        url = str(raw_data.get("jobUrl") or "").strip()
        if not title or not url:
            return None
        locations = raw_data.get("workingLocations") or []
        location = ", ".join(
            dict.fromkeys(
                str(
                    item.get("cityNameVI") or item.get("cityName") or item.get("address") or ""
                ).strip()
                for item in locations
                if isinstance(item, dict)
                and (item.get("cityNameVI") or item.get("cityName") or item.get("address"))
            )
        )
        skills = " ".join(
            str(item.get("skillName") or "")
            for item in (raw_data.get("skills") or [])
            if isinstance(item, dict)
        )
        return RawJob(
            title=title,
            company=str(raw_data.get("companyName") or ""),
            location=location,
            salary_raw=str(raw_data.get("prettySalary") or ""),
            description=f"{raw_data.get('jobDescription') or ''} {skills}",
            requirements=str(raw_data.get("jobRequirement") or ""),
            source=JobSource.VIETNAMWORKS,
            source_url=url,
            posted_at_raw=str(raw_data.get("approvedOn") or raw_data.get("createdOn") or ""),
        )
