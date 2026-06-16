"""
ViecLam24h.vn scraper.

Verified selectors:
- Job container: <a data-job-id="..."> elements (30 per page)
- Title: h3 (first one inside container, text-[16px])
- Company: h3 (second one, text-[14px] text-[#939295])
- Salary: span.text-[#2C95FF] with "triệu" text
- Location: span.text-se-neutral-80 or tooltip with city name
- Link: href attribute of the container <a> tag
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://vieclam24h.vn"


class ViecLam24hScraper(BaseScraper):
    """Scraper for ViecLam24h.vn (no Cloudflare, Tailwind CSS HTML)."""

    SEARCH_URL = f"{BASE_URL}/tim-kiem-viec-lam-nhanh"

    def __init__(self):
        super().__init__(source=JobSource.VIECLAM24H)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        max_pages = max_pages or self.settings.scrape_max_pages
        all_jobs: list[RawJob] = []

        for page in range(1, max_pages + 1):
            params = {"q": keyword, "page": page}

            try:
                response = self._get(self.SEARCH_URL, params=params)
                soup = BeautifulSoup(response.text, "html.parser")
                job_items = soup.find_all(attrs={"data-job-id": True})

                if not job_items:
                    logger.info(f"No more jobs on page {page}, stopping.")
                    break

                for item in job_items:
                    job = self.parse_job(item)
                    if job:
                        all_jobs.append(job)

                logger.info(
                    f"ViecLam24h page {page}: {len(job_items)} jobs",
                    extra={"source": self.source.value},
                )

            except Exception as e:
                logger.error(f"ViecLam24h page {page} failed: {e}", exc_info=True)
                break

        return all_jobs

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        """Parse a ViecLam24h job card (anchor with data-job-id).

        Structure:
        <a data-job-id="200813805" href="/..../id200813805.html">
          <h3 class="text-[16px]...">Job Title</h3>
          <h3 class="text-[14px]...text-[#939295]">Company Name</h3>
          <span class="text-[#2C95FF]">10 - 14 triệu</span>
          <span class="text-se-neutral-80">TP.HCM</span>
        </a>
        """
        try:
            # Link
            href = raw_data.get("href", "")
            source_url = urljoin(BASE_URL, href) if href else ""

            # Find all h3 tags
            h3_tags = raw_data.find_all("h3")

            # Title: first h3 (larger text, text-[16px])
            title = ""
            company = ""
            if h3_tags:
                title = h3_tags[0].get_text(strip=True)
            if len(h3_tags) > 1:
                company = h3_tags[1].get_text(strip=True)

            if not title:
                return None

            # Salary: span with blue color containing "triệu" or salary info
            salary = ""
            salary_spans = raw_data.find_all("span", string=re.compile(
                r"triệu|VNĐ|USD|thỏa thuận", re.IGNORECASE
            ))
            if salary_spans:
                salary = salary_spans[0].get_text(strip=True)

            # Location: span with class text-se-neutral-80 or tooltip-content
            location = ""
            loc_span = raw_data.find("span", class_="text-se-neutral-80")
            if not loc_span:
                loc_span = raw_data.find("span", class_="tooltip-content")
            if loc_span:
                location = loc_span.get_text(strip=True)

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                source=JobSource.VIECLAM24H,
                source_url=source_url,
            )

        except Exception as e:
            logger.warning(f"Failed to parse ViecLam24h job: {e}")
            return None
