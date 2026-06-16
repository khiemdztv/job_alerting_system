"""
Test scrapers live on the real websites.
"""
from __future__ import annotations

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scrapers.careerlink_scraper import CareerLinkScraper
from src.scrapers.vieclam24h_scraper import ViecLam24hScraper
from src.scrapers.itviec_scraper import ITviecScraper
from src.scrapers.careerviet_scraper import CareerVietScraper
from src.scrapers.timviec365_scraper import TimViec365Scraper
from src.scrapers.jooble_scraper import JoobleScraper
from src.etl.transformer import Transformer
from src.common.logger import get_logger

logger = get_logger(__name__)

def test_live():
    print("=== Testing CareerLink Scraper ===")
    cl = CareerLinkScraper()
    cl_jobs = cl.scrape("data engineer", max_pages=1)
    print(f"Scraped {len(cl_jobs)} raw jobs from CareerLink")
    if cl_jobs:
        print(f"First job: {cl_jobs[0].title} @ {cl_jobs[0].company} (Salary: {cl_jobs[0].salary_raw})")
    
    print("\n=== Testing ViecLam24h Scraper ===")
    vl = ViecLam24hScraper()
    vl_jobs = vl.scrape("data engineer", max_pages=1)
    print(f"Scraped {len(vl_jobs)} raw jobs from ViecLam24h")
    if vl_jobs:
        print(f"First job: {vl_jobs[0].title} @ {vl_jobs[0].company} (Salary: {vl_jobs[0].salary_raw})")

    print("\n=== Testing ITviec Scraper ===")
    it = ITviecScraper()
    it_jobs = it.scrape("data engineer", max_pages=1)
    print(f"Scraped {len(it_jobs)} raw jobs from ITviec")
    if it_jobs:
        print(f"First job: {it_jobs[0].title} @ {it_jobs[0].company} (Salary: {it_jobs[0].salary_raw})")

    print("\n=== Testing CareerViet Scraper ===")
    cv = CareerVietScraper()
    cv_jobs = cv.scrape("data engineer", max_pages=1)
    print(f"Scraped {len(cv_jobs)} raw jobs from CareerViet")
    if cv_jobs:
        print(f"First job: {cv_jobs[0].title} @ {cv_jobs[0].company} (Salary: {cv_jobs[0].salary_raw})")

    print("\n=== Testing TimViec365 Scraper ===")
    tv = TimViec365Scraper()
    tv_jobs = tv.scrape("data engineer", max_pages=1)
    print(f"Scraped {len(tv_jobs)} raw jobs from TimViec365")
    if tv_jobs:
        print(f"First job: {tv_jobs[0].title} @ {tv_jobs[0].company} (Salary: {tv_jobs[0].salary_raw})")

    print("\n=== Testing Jooble Scraper ===")
    jb = JoobleScraper()
    jb_jobs = jb.scrape("data engineer", max_pages=1)
    print(f"Scraped {len(jb_jobs)} raw jobs from Jooble")
    if jb_jobs:
        print(f"First job: {jb_jobs[0].title} @ {jb_jobs[0].company} (Salary: {jb_jobs[0].salary_raw})")

    print("\n=== Testing ETL Transformation ===")
    transformer = Transformer()
    all_raw = cl_jobs + vl_jobs + it_jobs + cv_jobs + tv_jobs + jb_jobs
    print(f"Total raw jobs: {len(all_raw)}")
    
    transformed_jobs = transformer.transform_batch(all_raw)
    print(f"Successfully transformed {len(transformed_jobs)} jobs")
    if transformed_jobs:
        job = transformed_jobs[0]
        print(f"Transformed Job Detail:")
        print(f"  ID: {job.job_id}")
        print(f"  Title: {job.title} (Normalized: {job.title_normalized})")
        print(f"  Company: {job.company}")
        print(f"  Location: {job.location} (Normalized: {job.location_normalized})")
        print(f"  Salary Min: {job.salary_min} VND, Max: {job.salary_max} VND")
        print(f"  Tags: {job.tags}")

if __name__ == "__main__":
    test_live()

