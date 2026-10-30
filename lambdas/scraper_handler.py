"""
Lambda Handler: Scraper

Triggered by EventBridge every 6 hours.
Runs all scrapers, pushes raw jobs to SQS for ETL processing.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# Add project root to path for Lambda
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.logger import get_logger
from src.common.scraper_health import (
    ScrapeResult,
    ScrapeStatus,
    is_paused,
    maybe_send_weekly_summary,
    record_health,
)
from src.config import get_settings
from src.data_quality.validators import RawJobValidator
from src.scrapers.base_scraper import LAYOUT_CHANGED, BaseScraper
from src.scrapers.registry import batch_scrapers

logger = get_logger(__name__)

# Seed keywords — multi-industry to ensure broad coverage.
# Rotated per run cycle to avoid Lambda timeout.
# User subscription keywords are ALWAYS added on top of these.
SEED_KEYWORD_GROUPS = [
    # Group 1: Business / Finance
    ["kế toán", "kinh doanh", "bán hàng", "tài chính", "ngân hàng"],
    # Group 2: Tech / IT
    ["software engineer", "data engineer", "lập trình", "it", "developer"],
    # Group 3: Services / Admin
    ["nhân sự", "marketing", "hành chính", "thiết kế", "truyền thông"],
    # Group 4: Industry / Specialist
    ["logistics", "xây dựng", "y tế", "giáo viên", "điều dưỡng"],
    # Group 5: Entry-level / F&B
    ["thực tập", "fresher", "bán hàng", "nhà hàng", "kho vận"],
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

    # Every registered board that is configured (see src/scrapers/registry.py).
    scrapers = batch_scrapers(settings)
    paused = [s.source.value for s in scrapers if is_paused(s.source.value, settings)]
    if paused:
        logger.warning("Skipping auto-paused sources: %s", ", ".join(paused))
        scrapers = [s for s in scrapers if s.source.value not in paused]
    maybe_send_weekly_summary(settings)
    logger.info(
        "Scraping %s sources: %s",
        len(scrapers),
        ", ".join(scraper.source.value for scraper in scrapers),
    )

    # Get keywords from subscriptions + defaults
    keywords = _get_active_keywords(settings)

    deadline = time.monotonic() + (
        context.get_remaining_time_in_millis() / 1000 - 45 if context else 840
    )
    total_raw_jobs = 0
    results = {name: {"status": "paused", "valid": 0} for name in paused}
    failures = []
    # One worker owns one source/session. Persist each keyword immediately.
    with ThreadPoolExecutor(max_workers=settings.scrape_workers) as pool:
        futures = {pool.submit(_scrape_source, scraper, keywords, settings, deadline):
                   scraper.source.value for scraper in scrapers}
        for future in as_completed(futures):
            source = futures[future]
            try:
                results[source] = future.result()
                total_raw_jobs += results[source]["valid"]
            except Exception:
                logger.exception("Source pipeline failed: %s", source)
                failures.append(source)
                results[source] = {"status": "error", "valid": 0}
    if failures:
        raise RuntimeError(f"Sources failed to persist jobs: {failures}")

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
    keywords = set()

    # Select seed keyword group based on hour (rotate every 6h)
    now = datetime.now(timezone.utc)
    cycle = int(now.timestamp() // (settings.alert_interval_hours * 3600))
    group_idx = cycle % len(SEED_KEYWORD_GROUPS)
    selected_group = SEED_KEYWORD_GROUPS[group_idx]
    logger.info(
        f"Using seed keyword group {group_idx + 1}/{len(SEED_KEYWORD_GROUPS)}: {selected_group}"
    )

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

        logger.info("Loaded %s subscription keywords", len(keywords))

    except Exception as e:
        logger.warning(f"Could not load subscription keywords: {e}")
        raise

    priority = sorted(keywords)
    # Rotate priorities across runs so a slow source cannot starve later keywords.
    if priority:
        offset = int(now.timestamp() // (settings.alert_interval_hours * 3600)) % len(priority)
        priority = priority[offset:] + priority[:offset]
    return priority + [kw for kw in selected_group if kw not in keywords]


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

            pending = entries
            for attempt in range(3):
                response = sqs.send_message_batch(QueueUrl=queue_url, Entries=pending)
                failed = {item["Id"] for item in response.get("Failed", [])}
                pending = [entry for entry in pending if entry["Id"] in failed]
                if not pending:
                    break
                if attempt < 2:
                    time.sleep(2 ** attempt)
            if pending:
                raise RuntimeError(f"SQS rejected {len(pending)} jobs after retries")

        logger.info(f"Pushed {len(raw_jobs)} jobs to SQS")

    except Exception as e:
        logger.error(f"SQS push failed: {e}")
        raise



def _save_raw_to_s3(raw_jobs, settings, source_name: str = "all_sources") -> None:
    """Save raw scraped data to S3 data lake."""
    try:
        import boto3

        s3 = boto3.client("s3", region_name=settings.aws_region)
        now = datetime.now(timezone.utc)

        data = [job.model_dump(mode="json") for job in raw_jobs]
        key = (
            f"raw/{source_name}/{now.strftime('%Y/%m/%d')}/"
            f"raw_{now.strftime('%Y%m%d_%H%M%S')}.json"
        )

        s3.put_object(
            Bucket=settings.s3_data_lake_bucket,
            Key=key,
            Body=json.dumps(data, ensure_ascii=False, default=str),
            ContentType="application/json",
        )

        logger.info(f"Saved {len(raw_jobs)} raw jobs to s3://{settings.s3_data_lake_bucket}/{key}")

    except Exception as e:
        logger.error(f"S3 save failed: {e}")


def _persist(jobs, scraper, validator, settings, errors):
    """Validate, archive and queue one batch; return how many were accepted."""
    valid, _ = validator.validate_batch(jobs)
    if scraper.last_error:
        errors.append(scraper.last_error)
    if valid:
        _save_raw_to_s3(valid, settings, scraper.source.value)
        _push_to_sqs(valid, settings)
    return len(valid)


_UNREACHABLE_ERRORS = ("ConnectTimeout", "ConnectionError", "SSLError", "ProxyError")


def _unreachable(errors) -> bool:
    """True when the source answered every attempt so far with a block or no connection."""
    attempts = [e for e in errors if e]
    return len(attempts) >= 2 and all(
        e.startswith("HTTP") or e in _UNREACHABLE_ERRORS for e in attempts
    )


def _scrape_source(scraper, keywords, settings, deadline):
    scraper.deadline_at = deadline
    validator = RawJobValidator()
    count, raw_count, errors = 0, 0, []
    started = time.monotonic()
    completed = 0
    # Boards with a browse-by-date page give every new posting in one pass,
    # independent of which keywords users happen to subscribe to.
    if getattr(scraper, "supports_latest", False) is True and settings.scrape_latest_pages > 0:
        scraper.last_error = ""
        try:
            latest = scraper.scrape_latest(max_pages=settings.scrape_latest_pages)
        except Exception as exc:
            logger.exception("scrape_latest failed for %s", scraper.source.value)
            scraper.last_error = scraper.last_error or type(exc).__name__
            latest = []
        raw_count += len(latest)
        count += _persist(latest, scraper, validator, settings, errors)
    for keyword in keywords:
        if time.monotonic() + 25 >= deadline:
            break
        jobs = scraper.scrape_safe(keyword, max_pages=settings.scrape_max_pages)
        raw_count += len(jobs)
        count += _persist(jobs, scraper, validator, settings, errors)
        completed += 1
        if count == 0 and _unreachable(errors):
            # Blocked (403/429) or unreachable from this network: every further
            # keyword would burn the same timeout, so leave the budget to others.
            logger.warning("Skipping remaining keywords for %s: %s",
                           scraper.source.value, errors[-1])
            break
    fallback_used = False
    fallback = getattr(scraper, "fallback", None)
    if count == 0 and isinstance(fallback, BaseScraper) and time.monotonic() + 60 < deadline:
        # The dedicated parser found nothing (layout change or empty page):
        # read the board's sitemap + JSON-LD instead so coverage survives.
        fallback_used = True
        fallback.deadline_at = deadline
        logger.warning("Using JSON-LD fallback for %s", scraper.source.value)
        try:
            latest = fallback.scrape_latest(max_pages=settings.scrape_latest_pages)
            raw_count += len(latest)
            count += _persist(latest, fallback, validator, settings, errors)
            for keyword in keywords[:3]:
                if time.monotonic() + 25 >= deadline:
                    break
                jobs = fallback.scrape_safe(keyword, max_pages=1)
                raw_count += len(jobs)
                count += _persist(jobs, fallback, validator, settings, errors)
        except Exception as exc:
            logger.exception("JSON-LD fallback failed for %s", scraper.source.value)
            errors.append(type(exc).__name__)
        finally:
            fallback.session.close()
    if count:
        status = ScrapeStatus.OK
    elif errors and all(error.startswith("HTTP") for error in errors):
        status = ScrapeStatus.BLOCKED  # e.g. HTTP403 from the Lambda IP range
    elif errors and all(error == LAYOUT_CHANGED for error in errors):
        status = ScrapeStatus.LAYOUT_CHANGED  # 200 pages, parser matched nothing
    elif errors:
        status = ScrapeStatus.ERROR
    else:
        status = ScrapeStatus.EMPTY
    record_health(ScrapeResult(
        source=scraper.source.value, keyword=",".join(keywords[:3]),
        status=status, job_count=count, error=",".join(sorted(set(errors))),
        duration_ms=int((time.monotonic() - started) * 1000),
    ), settings)
    scraper.session.close()
    return {"total_scraped": raw_count, "valid": count, "status": status.value,
            "keywords_completed": completed, "keywords_deferred": len(keywords) - completed,
            "fallback_used": fallback_used}
