"""
CareerViet.vn scraper.

CareerViet (formerly CareerBuilder Vietnam) is a large multi-industry job board.
Uses Next.js SSR — job data is available in the initial HTML response.

Verified selectors:
- Job container: div.job-item
- Title: a.job_link (text + data-id attribute)
- Company: a.company-name
- Salary: div.salary > p
- Location: div.location > ul > li
- Deadline: div.time time element
- Link: a.job_link[href] (prefix https://careerviet.vn)

URL format: https://careerviet.vn/viec-lam/{keyword}-k-vi.html
Pagination: ?page={n}
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup, Tag

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://careerviet.vn"


class CareerVietScraper(BaseScraper):
    """Scraper for CareerViet.vn (SSR Next.js, multi-industry job board)."""

    def __init__(self):
        super().__init__(source=JobSource.CAREERVIET)

    def _build_search_url(self, keyword: str, page: int) -> str:
        """Build CareerViet search URL.

        URL format: /viec-lam/{keyword}-k-vi.html?page={n}
        Keyword is URL-encoded with hyphens replacing spaces.
        """
        # CareerViet uses URL-encoded keyword in the path
        keyword_slug = quote(keyword.strip(), safe="")
        url = f"{BASE_URL}/viec-lam/{keyword_slug}-k-vi.html"
        if page > 1:
            url += f"?page={page}"
        return url

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Scrape CareerViet job listings.

        Args:
            keyword: Search keyword.
            max_pages: Max pages to scrape.

        Returns:
            List of RawJob objects.
        """
        max_pages = max_pages or self.settings.scrape_max_pages
        all_jobs: list[RawJob] = []

        for page in range(1, max_pages + 1):
            url = self._build_search_url(keyword, page)

            try:
                response = self._get(url)
                soup = BeautifulSoup(response.text, "html.parser")
                job_items = soup.find_all("div", class_="job-item")

                if not job_items:
                    logger.info(f"No more jobs on page {page}, stopping.")
                    break

                for item in job_items:
                    job = self.parse_job(item)
                    if job:
                        all_jobs.append(job)

                logger.info(
                    f"CareerViet page {page}: {len(job_items)} jobs found",
                    extra={"source": self.source.value},
                )

            except Exception as e:
                import requests as req_lib
                if isinstance(e, req_lib.HTTPError) and e.response is not None:
                    if e.response.status_code == 404:
                        logger.info(f"CareerViet returned 404 for keyword '{keyword}', no results.")
                        break
                logger.error(f"CareerViet page {page} failed: {e}", exc_info=True)
                break

        return all_jobs

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        """Parse a CareerViet job-item element.

        HTML structure:
        <div class="job-item" id="job-item-35C773A3">
          <div class="figure">
            <div class="figcaption">
              <div class="title">
                <h2><a class="job_link" data-id="..." href="...">Job Title</a></h2>
              </div>
              <div class="caption">
                <a class="company-name">Company Name</a>
                <div class="salary"><p>Lương: 20 Tr - 30 Tr VND</p></div>
                <div class="location"><ul><li>Hồ Chí Minh</li></ul></div>
                <div class="time"><time>23-06-2026</time></div>
              </div>
            </div>
          </div>
        </div>
        """
        try:
            # Title & Link
            link_el = raw_data.find("a", class_="job_link")
            if not link_el:
                return None

            title = link_el.get("title", "") or link_el.get_text(strip=True)
            if not title:
                return None

            href = link_el.get("href", "")
            source_url = urljoin(BASE_URL, href) if href else ""

            # Company
            company_el = raw_data.find("a", class_="company-name")
            company = ""
            if company_el:
                company = company_el.get("title", "") or company_el.get_text(strip=True)

            # Salary
            salary = ""
            salary_el = raw_data.find("div", class_="salary")
            if salary_el:
                salary_text = salary_el.get_text(strip=True)
                # Remove "Lương:" or "Luong:" prefix
                salary = re.sub(r"^(Lương|Luong)\s*:\s*", "", salary_text, flags=re.IGNORECASE).strip()

            # Location
            location = ""
            loc_el = raw_data.find("div", class_="location")
            if loc_el:
                loc_items = loc_el.find_all("li")
                if loc_items:
                    location = ", ".join(li.get_text(strip=True) for li in loc_items)
                else:
                    location = loc_el.get_text(strip=True)

            # Deadline / Posted date
            posted_at_raw = ""
            time_el = raw_data.find("time")
            if time_el:
                posted_at_raw = time_el.get_text(strip=True)

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                source=JobSource.CAREERVIET,
                source_url=source_url,
                posted_at_raw=posted_at_raw,
            )

        except Exception as e:
            logger.warning(f"Failed to parse CareerViet job: {e}")
            return None
