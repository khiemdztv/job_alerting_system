"""
Lambda Handler: Scraper

Triggered by EventBridge every 6 hours.
Runs all scrapers, pushes raw jobs to SQS for ETL processing.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

# Add project root to path for Lambda
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.logger import get_logger
from src.common.models import JobSource
from src.config import get_settings
from src.data_quality.validators import RawJobValidator
from src.scrapers.careerlink_scraper import CareerLinkScraper
from src.scrapers.careerviet_scraper import CareerVietScraper
from src.scrapers.itviec_scraper import ITviecScraper
from src.scrapers.jooble_scraper import JoobleScraper
from src.scrapers.timviec365_scraper import TimViec365Scraper
from src.scrapers.vieclam24h_scraper import ViecLam24hScraper
from src.scrapers.ybox_scraper import YBoxScraper

logger = get_logger(__name__)

# Seed keywords — kept minimal to avoid Lambda timeout.
# User subscription keywords are ALWAYS added on top of these.
SEED_KEYWORDS = [
    "software engineer",
    "data engineer",
    "kế toán",
    "marketing",
    "nhân sự",
]


def handler(event, context):
    """Lambda handler for scraping all sources.

    Args:
        event: EventBridge scheduled event.
        context: Lambda context.

    Returns:
        Summary of scraping results.
    """
    settings = get_settings()
    start_time = datetime.now(timezone.utc)

    logger.info("Starting scraper run", extra={"source": "all"})

    # Initialize scrapers (all active sources)
    scrapers = [
        CareerLinkScraper(),
        ViecLam24hScraper(),
        ITviecScraper(),
        CareerVietScraper(),
        TimViec365Scraper(),
        YBoxScraper(),
    ]

    # Add Jooble if API key is configured
    if settings.jooble_api_key:
        scrapers.append(JoobleScraper())
    else:
        logger.warning("Jooble API key not set, skipping Jooble scraper")

    # Get keywords from subscriptions + defaults
    keywords = _get_active_keywords(settings)

    # Validate
    validator = RawJobValidator()

    # Scrape all sources for all keywords.
    # IMPORTANT: Push to SQS after EACH source to avoid losing data on timeout.
    total_raw_jobs = 0
    results = {}

    for scraper in scrapers:
        source_name = scraper.source.value
        source_jobs = []

        for keyword in keywords:
            jobs = scraper.scrape_safe(keyword, max_pages=1)
            source_jobs.extend(jobs)

        # Validate
        valid_jobs, quality_report = validator.validate_batch(source_jobs)

        results[source_name] = {
            "total_scraped": len(source_jobs),
            "valid": len(valid_jobs),
            "pass_rate": quality_report.pass_rate,
        }

        logger.info(
            f"{source_name}: scraped {len(source_jobs)}, valid {len(valid_jobs)}",
            extra={"source": source_name, "job_count": len(valid_jobs)},
        )

        # Push this source's jobs to SQS immediately (stream-style)
        if valid_jobs:
            _push_to_sqs(valid_jobs, settings)
            _save_raw_to_s3(valid_jobs, settings, source_name)
            total_raw_jobs += len(valid_jobs)

    duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)

    summary = {
        "status": "success",
        "total_raw_jobs": total_raw_jobs,
        "sources": results,
        "keywords_searched": len(keywords),
        "duration_ms": duration_ms,
        "timestamp": start_time.isoformat(),
    }

    logger.info(
        f"Scraper run complete: {total_raw_jobs} total jobs",
        extra={"job_count": total_raw_jobs, "duration_ms": duration_ms},
    )

    return summary


def _get_active_keywords(settings) -> list[str]:
    """Get unique keywords: user subscriptions (priority) + seed keywords.

    User-subscribed keywords are ALWAYS included, ensuring any industry
    a user subscribes to will be scraped automatically.
    """
    # Start with seed keywords for baseline multi-industry coverage
    keywords = set(SEED_KEYWORDS)

    try:
        import boto3

        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(settings.dynamodb_users_table)

        response = table.scan(
            FilterExpression="begins_with(sk, :prefix) AND is_active = :active",
            ExpressionAttributeValues={":prefix": "SUB#", ":active": True},
            ProjectionExpression="keyword_normalized",
        )

        for item in response.get("Items", []):
            kw = item.get("keyword_normalized", "")
            if kw:
                keywords.add(kw)

        # Handle pagination
        while "LastEvaluatedKey" in response:
            response = table.scan(
                FilterExpression="begins_with(sk, :prefix) AND is_active = :active",
                ExpressionAttributeValues={":prefix": "SUB#", ":active": True},
                ProjectionExpression="keyword_normalized",
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            for item in response.get("Items", []):
                kw = item.get("keyword_normalized", "")
                if kw:
                    keywords.add(kw)

        logger.info(f"Loaded {len(keywords)} keywords ({len(keywords) - len(SEED_KEYWORDS)} from subscriptions)")

    except Exception as e:
        logger.warning(f"Could not load subscription keywords: {e}")

    return list(keywords)


def _push_to_sqs(raw_jobs, settings) -> None:
    """Push raw jobs to SQS for ETL processing."""
    try:
        import boto3

        sqs = boto3.client("sqs", region_name=settings.aws_region)
        queue_url = sqs.get_queue_url(QueueName=settings.sqs_raw_jobs_queue)["QueueUrl"]

        # Batch into groups of 10 (SQS limit)
        batch_size = 10
        for i in range(0, len(raw_jobs), batch_size):
            batch = raw_jobs[i : i + batch_size]
            entries = []

            for j, job in enumerate(batch):
                entries.append({
                    "Id": str(j),
                    "MessageBody": json.dumps(
                        job.model_dump(mode="json"),
                        ensure_ascii=False,
                        default=str,
                    ),
                })

            sqs.send_message_batch(
                QueueUrl=queue_url,
                Entries=entries,
            )

        logger.info(f"Pushed {len(raw_jobs)} jobs to SQS")

    except Exception as e:
        logger.error(f"SQS push failed: {e}")



def _save_raw_to_s3(raw_jobs, settings, source_name: str = "all_sources") -> None:
    """Save raw scraped data to S3 data lake."""
    try:
        import boto3

        s3 = boto3.client("s3", region_name=settings.aws_region)
        now = datetime.now(timezone.utc)

        data = [job.model_dump(mode="json") for job in raw_jobs]
        key = f"raw/{source_name}/{now.strftime('%Y/%m/%d')}/raw_{now.strftime('%Y%m%d_%H%M%S')}.json"

        s3.put_object(
            Bucket=settings.s3_data_lake_bucket,
            Key=key,
            Body=json.dumps(data, ensure_ascii=False, default=str),
            ContentType="application/json",
        )

        logger.info(f"Saved {len(raw_jobs)} raw jobs to s3://{settings.s3_data_lake_bucket}/{key}")

    except Exception as e:
        logger.error(f"S3 save failed: {e}")
