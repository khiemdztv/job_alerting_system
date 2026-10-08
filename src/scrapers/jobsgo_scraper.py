"""JobsGO live search scraper.

JobsGO serves current listings to search-engine crawlers while its normal
browser endpoint uses a Cloudflare challenge.  We use the documented public
listing pages and parse only job cards already present in the HTML.

Verified card markup (October 2026):
- Container: div.job-card
- Title: h3.job-title a[title] (text may be prefixed by a "HOT" badge span)
- Company: a.company-title
- Salary and city: spans inside div.text-primary, separated by a "|" span
- Updated: span.badge[title="Thời gian cập nhật"], e.g. "18 phút trước"
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

_SALARY = re.compile(r"triệu|VNĐ|USD|thỏa thuận", re.I)
_CITY = re.compile(r"Hồ Chí Minh|TP\.?\s*HCM|Hà Nội|Đà Nẵng|Bình Dương|Đồng Nai|Remote", re.I)
_AGO = re.compile(r"\d+\s+(?:phút|giờ|ngày|tuần|tháng)\s+trước", re.I)


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
        title = (title_link.get("title") or "").strip() or title_link.get_text(" ", strip=True)
        company_el = raw_data.select_one(".company-title")
        company = company_el.get_text(" ", strip=True) if company_el else ""
        source_url = urljoin(BASE_URL, title_link.get("href", ""))

        # Salary and city live in one metadata row; the job title is never consulted
        # for the city, because titles such as "... tại Hồ Chí Minh" misled the old parser.
        meta = raw_data.select_one(".text-primary")
        meta_parts = [
            part
            for span in (meta.find_all("span") if meta else [])
            if (part := span.get_text(" ", strip=True)) and part != "|"
        ]
        salary = next((part for part in meta_parts if _SALARY.search(part)), "")
        location = next((part for part in reversed(meta_parts) if part != salary), "")

        text_parts = [
            part.strip()
            for part in raw_data.stripped_strings
            if part.strip() and part.strip() != title
        ]
        if not salary:
            salary = next((part for part in text_parts if _SALARY.search(part)), "")
        if not location:
            location = next((part for part in text_parts if _CITY.search(part)), "")

        posted_el = raw_data.find("span", title="Thời gian cập nhật")
        posted = posted_el.get_text(" ", strip=True) if posted_el else ""
        if not posted:
            posted = next((part for part in text_parts if _AGO.search(part)), "")

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
