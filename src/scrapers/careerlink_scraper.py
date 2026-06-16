"""
CareerLink.vn scraper.

Selectors (verified from live HTML):
- Job container: .job-item
- Title: .job-name > a.job-link
- Company: a.job-company
- Location: .job-location
- Salary: .job-salary
- Link: a.job-link[href]
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://www.careerlink.vn"


class CareerLinkScraper(BaseScraper):
    """Scraper for CareerLink.vn (no Cloudflare, HTML parsing)."""

    SEARCH_URL = f"{BASE_URL}/vieclam/list"

    def __init__(self):
        super().__init__(source=JobSource.CAREERLINK)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Scrape CareerLink job listings.

        Args:
            keyword: Search keyword.
            max_pages: Max pages to scrape.

        Returns:
            List of RawJob objects.
        """
        max_pages = max_pages or self.settings.scrape_max_pages
        all_jobs: list[RawJob] = []

        for page in range(1, max_pages + 1):
            params = {
                "keyword": keyword,
                "page": page,
            }

            try:
                response = self._get(self.SEARCH_URL, params=params)
                soup = BeautifulSoup(response.text, "html.parser")
                job_items = soup.find_all(class_="job-item")

                if not job_items:
                    logger.info(f"No more jobs on page {page}, stopping.")
                    break

                for item in job_items:
                    job = self.parse_job(item)
                    if job:
                        all_jobs.append(job)

                logger.info(
                    f"CareerLink page {page}: {len(job_items)} jobs found",
                    extra={"source": self.source.value},
                )

            except Exception as e:
                logger.error(f"CareerLink page {page} failed: {e}", exc_info=True)
                break

        return all_jobs

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        """Parse a CareerLink job-item element.

        HTML structure:
        <li class="job-item">
          <a class="job-link" href="/tim-viec-lam/data-engineer/3534173">
            <h5 class="job-name">DATA ENGINEER</h5>
          </a>
          <a class="job-company">COMPANY NAME</a>
          <div class="job-location">Hà Nội</div>
          <div class="job-salary">15 - 25 triệu</div>
        </li>
        """
        try:
            # Title & Link
            link_el = raw_data.find("a", class_="job-link")
            if not link_el:
                return None

            title = link_el.get_text(strip=True)
            href = link_el.get("href", "")
            source_url = urljoin(BASE_URL, href) if href else ""

            if not title:
                return None

            # Company
            company_el = raw_data.find("a", class_="job-company")
            company = company_el.get_text(strip=True) if company_el else ""

            # Location
            location_el = raw_data.find(class_="job-location")
            location = location_el.get_text(strip=True) if location_el else ""

            # Salary
            salary_el = raw_data.find(class_="job-salary")
            salary = salary_el.get_text(strip=True) if salary_el else ""

            # Position/level
            position_el = raw_data.find(class_="job-position")
            position = position_el.get_text(strip=True) if position_el else ""

            # Date posted
            posted_at_raw = ""
            date_el = raw_data.find(class_="cl-datetime")
            if date_el and date_el.get("data-datetime"):
                try:
                    ts = int(date_el.get("data-datetime"))
                    from datetime import datetime, timezone
                    posted_at_raw = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except Exception:
                    pass

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                description=position,  # Use position as additional info
                source=JobSource.CAREERLINK,
                source_url=source_url,
                posted_at_raw=posted_at_raw,
            )

        except Exception as e:
            logger.warning(f"Failed to parse CareerLink job: {e}")
            return None
