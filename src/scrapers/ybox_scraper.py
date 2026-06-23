"""
Ybox.vn scraper.
Matches the site's client-side search logic by parsing window.__INITIAL_ADS__ and filtering locally.
"""

from __future__ import annotations

import json
import re
from typing import Optional
from bs4 import BeautifulSoup

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

logger = get_logger(__name__)

BASE_URL = "https://ybox.vn"


class YBoxScraper(BaseScraper):
    """Scraper for YBOX (ybox.vn).

    Fetches search results by downloading the search HTML page
    and parsing the `window.__INITIAL_ADS__` variable.
    """

    SEARCH_URL = f"{BASE_URL}/tuyen-dung-viec-lam-tk-c1"

    def __init__(self):
        super().__init__(source=JobSource.YBOX)

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Scrape YBOX job listings.

        Ybox search returns all ads on a single page, which is filtered client-side.
        We fetch the page with keyword search parameter to simulate the search,
        extract INITIAL_ADS JSON data, and filter results locally.
        """
        all_jobs: list[RawJob] = []

        params = {
            "keyword": keyword,
        }

        try:
            response = self._get(self.SEARCH_URL, params=params)
            response.encoding = "utf-8"
            soup = BeautifulSoup(response.text, "html.parser")

            ads_data = None
            for script in soup.find_all("script"):
                if script.string and "window.__INITIAL_ADS__" in script.string:
                    content = script.string.strip()
                    # Find start and end indices of the JSON object
                    start_idx = content.find("{")
                    end_idx = content.rfind("}")
                    if start_idx != -1 and end_idx != -1:
                        try:
                            ads_data = json.loads(content[start_idx:end_idx+1])
                            break
                        except Exception as je:
                            logger.error(f"Failed to parse Ybox INITIAL_ADS JSON: {je}")

            if not ads_data:
                logger.warning("Could not find or parse Ybox INITIAL_ADS script tag.")
                return []

            edges = ads_data.get("Ads", {}).get("edges", [])
            logger.info(f"Ybox search page returned {len(edges)} ads. Filtering for keyword '{keyword}'...")

            # Clean keyword for comparison
            kw_clean = keyword.lower().strip()

            for edge in edges:
                post = edge.get("post")
                if not post:
                    continue

                title = post.get("title", "")
                summary = post.get("summary", "")

                # Filter locally: check if keyword is in title or description/summary
                if (kw_clean in title.lower()) or (kw_clean in summary.lower()):
                    job = self.parse_job(post)
                    if job:
                        all_jobs.append(job)

            logger.info(
                f"YBox: {len(all_jobs)} jobs matched keyword '{keyword}'",
                extra={"source": self.source.value},
            )

        except Exception as e:
            logger.error(f"YBox scraping failed: {e}", exc_info=True)

        return all_jobs

    def parse_job(self, post: dict) -> Optional[RawJob]:
        """Parse Ybox post dictionary to RawJob."""
        try:
            title = post.get("title", "").strip()
            if not title:
                return None

            slug = post.get("slug", "")
            if not slug:
                return None

            source_url = f"{BASE_URL}/tuyen-dung/{slug}"

            # Publisher info (company)
            publisher = post.get("publisher") or {}
            company = publisher.get("fullName") or publisher.get("username") or "Ybox Employer"

            # Parse location from title (e.g. "[HCM]...", "[HN]...", "[Đà Nẵng]")
            # Common pattern is bracketed prefix
            location = "Việt Nam"
            loc_match = re.search(r"^\[([^\]]+)\]", title)
            if loc_match:
                location = loc_match.group(1).strip()
            else:
                # Fallback: check text for HN or HCM
                if "hcm" in title.lower() or "hồ chí minh" in title.lower() or "sài gòn" in title.lower():
                    location = "Hồ Chí Minh"
                elif "hn" in title.lower() or "hà nội" in title.lower():
                    location = "Hà Nội"

            # Parse salary from title if specified (e.g. "Thu nhập 10-20 Triệu" or "Mức lương 8-10 triệu")
            salary = "Thỏa thuận"
            # Match formats like "10 - 20 Triệu", "10-20tr", "10 triệu", "3-5 Triệu"
            salary_match = re.search(r"(?:lương|thu nhập)[:\s]*([0-9\s\-\.\,trTriệu]+(?:\+/)?(?:\s*tháng)?)", title, re.IGNORECASE)
            if salary_match:
                salary = salary_match.group(1).strip()
            else:
                # Check for "triệu" in title
                salary_match_2 = re.search(r"([0-9\s\-\.\,tr]+tr(?:iệu)?(?:/tháng)?)", title, re.IGNORECASE)
                if salary_match_2:
                    salary = salary_match_2.group(1).strip()

            description = post.get("summary", "").strip()
            posted_at_raw = post.get("publishedAt", "") or post.get("acceptedAt", "")

            return RawJob(
                title=title,
                company=company,
                location=location,
                salary_raw=salary,
                description=description,
                source=JobSource.YBOX,
                source_url=source_url,
                posted_at_raw=posted_at_raw,
            )

        except Exception as e:
            logger.warning(f"Failed to parse Ybox job: {e}")
            return None
