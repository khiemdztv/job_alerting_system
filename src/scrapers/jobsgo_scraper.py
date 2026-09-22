"""JobsGO live search scraper.

JobsGO serves current listings to search-engine crawlers while its normal
browser endpoint uses a Cloudflare challenge.  We use the documented public
listing pages and parse only job cards already present in the HTML.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from src.common.models import JobSource, RawJob
from src.common.text_utils import slugify_vn
from src.scrapers.base_scraper import BaseScraper

BASE_URL = "https://jobsgo.vn"


class JobsGoScraper(BaseScraper):
    def __init__(self):
        super().__init__(source=JobSource.JOBSGO)
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        )

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        max_pages = max_pages or self.settings.scrape_max_pages
        jobs: list[RawJob] = []
        path = f"{BASE_URL}/viec-lam-{slugify_vn(keyword)}.html"
        for page in range(1, max_pages + 1):
            response = self._get(path, params={"page": page} if page > 1 else None)
            cards = BeautifulSoup(response.text, "html.parser").select(".job-card")
            if not cards:
                break
            jobs.extend(job for card in cards if (job := self.parse_job(card)))
        return jobs

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        title_link = raw_data.select_one(".job-title a[href]")
        if not title_link:
            return None
        title = title_link.get_text(" ", strip=True)
        company_el = raw_data.select_one(".company-title")
        company = company_el.get_text(" ", strip=True) if company_el else ""
        source_url = urljoin(BASE_URL, title_link.get("href", ""))

        text_parts = [part.strip() for part in raw_data.stripped_strings if part.strip()]
        salary = next(
            (part for part in text_parts if re.search(r"triệu|VNĐ|USD|thỏa thuận", part, re.I)),
            "",
        )
        location = next(
            (
                part
                for part in text_parts
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
                for part in text_parts
                if re.search(r"\d+\s+(?:giờ|ngày|tuần|tháng)\s+trước", part, re.I)
            ),
            "",
        )
        return RawJob(
            title=title,
            company=company,
            location=location,
            salary_raw=salary,
            description=" ".join(text_parts),
            source=JobSource.JOBSGO,
            source_url=source_url,
            posted_at_raw=posted,
        )
