"""
TimViec365.vn scraper.

TimViec365 is a large multi-industry job board with 500K+ listings.
Returns static HTML — easy to parse with BeautifulSoup.

Verified structure (from live HTML):
- Each job is an <h2> heading followed by company, location, salary info
- URL format: https://timviec365.vn/viec-lam?key={keyword}&page={n}
- Job links: <a href="/job-slug-p{id}.html">
- Company links: <a href="/company-slug-co{id}">
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

BASE_URL = "https://timviec365.vn"


class TimViec365Scraper(BaseScraper):
    """Scraper for TimViec365.vn (static HTML, multi-industry job board)."""

    SEARCH_URL = f"{BASE_URL}/viec-lam"

    def __init__(self):
        super().__init__(source=JobSource.TIMVIEC365)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Scrape TimViec365 job listings.

        Args:
            keyword: Search keyword.
            max_pages: Max pages to scrape.

        Returns:
            List of RawJob objects.
        """
        max_pages = max_pages or self.settings.scrape_max_pages
        all_jobs: list[RawJob] = []

        for page in range(1, max_pages + 1):
            params = {"key": keyword, "page": page}

            try:
                response = self._get(self.SEARCH_URL, params=params)
                soup = BeautifulSoup(response.text, "html.parser")

                # TimViec365 job listings are structured with job links
                # that follow the pattern: /job-slug-p{id}.html
                job_links = soup.find_all("a", href=re.compile(r"-p\d+\.html$"))

                if not job_links:
                    logger.info(f"No more jobs on page {page}, stopping.")
                    break

                # Deduplicate links (same job appears twice in the HTML)
                seen_hrefs = set()
                unique_links = []
                for link in job_links:
                    href = link.get("href", "")
                    if href and href not in seen_hrefs:
                        seen_hrefs.add(href)
                        unique_links.append(link)

                for link in unique_links:
                    job = self.parse_job(link)
                    if job:
                        all_jobs.append(job)

                logger.info(
                    f"TimViec365 page {page}: {len(unique_links)} jobs found",
                    extra={"source": self.source.value},
                )

            except Exception as e:
                logger.error(f"TimViec365 page {page} failed: {e}", exc_info=True)
                break

        return all_jobs

    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        """Parse a TimViec365 job link element.

        TimViec365 structure (from markdown rendering of the page):
        Each job block contains:
        - <a href="/job-slug-p{id}.html">Job Title</a>
        - Nearby: company name, location, salary, deadline

        The parent container holds all the job info.
        """
        try:
            # Title & Link
            title = raw_data.get_text(strip=True)
            if not title or len(title) < 3:
                return None

            href = raw_data.get("href", "")
            source_url = urljoin(BASE_URL, href) if href else ""

            # Navigate to parent container to find related info
            # The job card parent typically contains company, location, salary
            parent = raw_data.parent
            if parent:
                # Go up to the job card container
                card = parent.parent if parent.parent else parent
            else:
                card = raw_data

            # Company: look for company link (href contains "-co" pattern)
            company = ""
            company_links = card.find_all("a", href=re.compile(r"-co\d+")) if card else []
            if company_links:
                company = company_links[0].get_text(strip=True)

            # Location & Salary: extract from sibling/nearby text elements
            location = ""
            salary = ""
            posted_at_raw = ""

            # Get all text in the card to extract info
            if card:
                card_text = card.get_text(" | ", strip=True)

                # Location patterns (Vietnamese city names)
                location_patterns = [
                    "Hà Nội", "Hồ Chí Minh", "Đà Nẵng", "Hải Phòng",
                    "Cần Thơ", "Bình Dương", "Đồng Nai", "Bắc Ninh",
                    "Hưng Yên", "Khánh Hòa", "Lâm Đồng", "Nghệ An",
                    "Thanh Hóa", "Quảng Ninh", "Thái Nguyên", "Ninh Bình",
                    "Vĩnh Long", "Tuyên Quang", "Tây Ninh", "Phú Thọ",
                ]
                for loc in location_patterns:
                    if loc in card_text:
                        location = loc
                        break

                # Salary: look for "triệu" or salary patterns
                salary_match = re.search(
                    r"(\d+\s*-\s*\d+\s*triệu|Từ\s*\d+\s*triệu|Đến\s*\d+\s*triệu|Thỏa\s*Thuận|\d+\s*-\s*\d+\s*VNĐ)",
                    card_text,
                    re.IGNORECASE,
                )
                if salary_match:
                    salary = salary_match.group(0).strip()

                # Date: look for dd/mm/yyyy pattern
                date_match = re.search(r"\d{2}/\d{2}/\d{4}", card_text)
                if date_match:
                    posted_at_raw = date_match.group(0)

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                source=JobSource.TIMVIEC365,
                source_url=source_url,
                posted_at_raw=posted_at_raw,
            )

        except Exception as e:
            logger.warning(f"Failed to parse TimViec365 job: {e}")
            return None
