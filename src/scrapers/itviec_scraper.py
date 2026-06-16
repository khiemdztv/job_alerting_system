"""
ITviec.com scraper.

ITviec has both HTML job-card elements AND JSON-LD structured data.
Strategy: Use .job-card elements with Stimulus data attributes.

Verified selectors:
- Job container: div.job-card (20 per page)
- Title: h3 inside job-card
- Job slug: data-search--job-selection-job-slug-value attribute
- Company: text from employer link
- Tags/skills: extracted from card text
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

BASE_URL = "https://itviec.com"


class ITviecScraper(BaseScraper):
    """Scraper for ITviec.com (IT-focused job board, has CF scripts but doesn't block)."""

    SEARCH_URL = f"{BASE_URL}/it-jobs"

    def __init__(self):
        super().__init__(source=JobSource.ITVIEC)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Scrape ITviec job listings.

        Args:
            keyword: Search keyword.
            max_pages: Max pages to scrape.

        Returns:
            List of RawJob objects.
        """
        max_pages = max_pages or min(self.settings.scrape_max_pages, 3)  # Gentle on ITviec
        all_jobs: list[RawJob] = []

        for page in range(1, max_pages + 1):
            params = {
                "query": keyword,
                "page": page,
            }

            try:
                response = self._get(self.SEARCH_URL, params=params)
                soup = BeautifulSoup(response.text, "html.parser")
                job_cards = soup.find_all(class_="job-card")

                if not job_cards:
                    logger.info(f"No more jobs on page {page}, stopping.")
                    break

                for card in job_cards:
                    job = self.parse_job(card)
                    if job:
                        all_jobs.append(job)

                logger.info(
                    f"ITviec page {page}: {len(job_cards)} jobs found",
                    extra={"source": self.source.value},
                )

            except Exception as e:
                import requests
                if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code in [403, 404]:
                    if e.response.status_code == 403:
                        logger.warning("ITviec returned 403 (Cloudflare protection active). Skipping ITviec scraper.")
                    else:
                        logger.warning(f"ITviec returned 404 (non-existent route for keyword '{keyword}'). Skipping ITviec scraper.")
                else:
                    logger.error(f"ITviec page {page} failed: {e}", exc_info=True)
                break

        return all_jobs


    def parse_job(self, raw_data: Tag) -> Optional[RawJob]:
        """Parse an ITviec job-card element.

        ITviec uses Stimulus.js with rich data attributes:
        - data-search--job-selection-job-slug-value: job URL slug
        - data-search--job-selection-job-url-value: full content URL

        HTML structure:
        <div class="job-card" data-search--job-selection-job-slug-value="...">
          <h3>Job Title</h3>
          ...company info...
          ...skills tags...
        </div>
        """
        try:
            # Title
            title_el = raw_data.find("h3")
            if not title_el:
                return None
            title = title_el.get_text(strip=True)
            if not title:
                return None

            # URL from data attribute
            slug = raw_data.get("data-search--job-selection-job-slug-value", "")
            source_url = f"{BASE_URL}/it-jobs/{slug}" if slug else ""

            # Company - find employer link
            company = ""
            # ITviec employer links typically have specific patterns
            all_links = raw_data.find_all("a")
            for link in all_links:
                href = link.get("href", "")
                if "/companies/" in href or "/nha-tuyen-dung/" in href:
                    text_val = link.get_text(strip=True)
                    if text_val:
                        company = text_val
                        break

            # If no company link found, try to extract from card text
            if not company:
                card_text = raw_data.get_text(" | ", strip=True)
                # Company usually appears after title
                parts = card_text.split("|")
                # Filter out metadata parts
                filtered_parts = [
                    p.strip() for p in parts
                    if p.strip() and not any(kw in p.lower() for kw in ["hot", "posted", "ago", "ngày", "giờ", "trước"])
                ]
                if len(filtered_parts) > 1:
                    company = filtered_parts[1]
                elif filtered_parts:
                    company = filtered_parts[0]

            # Date posted
            posted_at_raw = ""
            date_el = raw_data.find("span", class_=lambda c: c and "text-dark-grey" in c if c else False)
            if not date_el:
                date_el = raw_data.find(class_=lambda c: c and "small-text" in c if c else False)
            if date_el and "posted" in date_el.get_text().lower():
                posted_text = date_el.get_text().strip()
                posted_at_raw = re.sub(r"\s+", " ", posted_text)

            # Salary
            salary = ""
            salary_el = raw_data.find(string=re.compile(r"salary|lương", re.IGNORECASE))
            if salary_el:
                salary = salary_el.strip()
            # Also check for sign-in-to-view pattern
            sign_in_salary = raw_data.find(class_=lambda c: c and "salary" in " ".join(c).lower() if c else False)
            if sign_in_salary:
                salary = sign_in_salary.get_text(strip=True)

            # Location
            location = ""
            location_patterns = ["Ha Noi", "Ho Chi Minh", "Da Nang", "Hà Nội", "Hồ Chí Minh", "Đà Nẵng"]
            card_text = raw_data.get_text(" ", strip=True)
            for loc in location_patterns:
                if loc.lower() in card_text.lower():
                    location = loc
                    break

            # Skills/tags
            tags_text = ""
            tag_elements = [
                l.get_text(strip=True)
                for l in raw_data.find_all("a")
                if "Skill+tag" in l.get("href", "") or (
                    l.get("class") and any("itag" in cls.lower() for cls in l.get("class"))
                )
            ]
            if tag_elements:
                tags_text = ", ".join(tag_elements)

            # Job type (remote/at-office)
            job_type = ""
            if "remote" in card_text.lower():
                job_type = "Remote"
            elif "at office" in card_text.lower():
                job_type = "At office"

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                description=tags_text,
                job_type_raw=job_type,
                source=JobSource.ITVIEC,
                source_url=source_url,
                posted_at_raw=posted_at_raw,
            )

        except Exception as e:
            logger.warning(f"Failed to parse ITviec job: {e}")
            return None
