"""
Run the entire end-to-end Data Engineering pipeline locally using real AWS resources.
1. Runs live scrapers (CareerLink & ViecLam24h) for active user subscription keywords + defaults.
2. Validates raw jobs and pushes them to SQS.
3. Pulls jobs from SQS, transforms, deduplicates, and loads them to DynamoDB & S3.
4. Matches the newly scraped jobs against active subscriptions.
5. Sends real-time alerts to subscribed users via the Telegram Bot API.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import boto3

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lambdas.etl_handler import handler as etl_handler
from lambdas.matcher_handler import handler as matcher_handler
from lambdas.scraper_handler import SEED_KEYWORD_GROUPS, _scrape_source
from lambdas.scraper_handler import handler as scraper_handler
from src.config import get_settings
from src.scrapers.registry import batch_scrapers

# Budget for one external run; the GitHub workflow itself is capped at 20 minutes.
EXTERNAL_RUN_SECONDS = 15 * 60


def _split(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def run_selected_scrapers(sources, keywords, latest_only):
    """Scrape only ``sources``, persist to S3 and push to SQS (no ETL, no matcher).

    Used by the external runner for boards that block the AWS Lambda IP range.
    A failing source is reported and skipped; the others still run.
    """
    settings = get_settings()
    wanted = set(sources)
    scrapers = [s for s in batch_scrapers(settings) if s.source.value in wanted]
    unknown = wanted - {s.source.value for s in scrapers}
    if unknown:
        print(f"Unknown or unavailable sources ignored: {sorted(unknown)}")
    if latest_only:
        keywords = []
    elif not keywords:
        cycle = int(time.time() // (settings.alert_interval_hours * 3600))
        keywords = list(SEED_KEYWORD_GROUPS[cycle % len(SEED_KEYWORD_GROUPS)])
    deadline = time.monotonic() + EXTERNAL_RUN_SECONDS
    results, total = {}, 0
    for scraper in scrapers:
        name = scraper.source.value
        try:
            results[name] = _scrape_source(scraper, keywords, settings, deadline)
            total += results[name]["valid"]
        except Exception as exc:  # continue-on-error per source
            print(f"Source {name} failed: {type(exc).__name__}: {exc}")
            results[name] = {"status": "error", "valid": 0, "error": type(exc).__name__}
    return {
        "status": "success",
        "total_raw_jobs": total,
        "sources": results,
        "keywords_searched": len(keywords),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def run_pipeline(args=None):
    settings = get_settings()
    region = settings.aws_region
    sources = _split(getattr(args, "sources", ""))
    if sources:
        summary = run_selected_scrapers(
            sources, _split(args.keywords), args.latest_only
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    print("=" * 60)
    print("STARTING END-TO-END PIPELINE RUN (LIVE)")
    print("=" * 60)

    # --- PHASE 1: Run Scrapers & Push to SQS ---
    print("\n[STEP 1/4] Running live Scrapers (CareerLink & ViecLam24h)...")
    # This will scrape, validate, push to SQS queue, and save raw to S3
    scraper_summary = scraper_handler(None, None)
    print(f"Scraper Run Summary: {json.dumps(scraper_summary, indent=2, ensure_ascii=False)}")

    if scraper_summary.get("total_raw_jobs", 0) == 0:
        print("No jobs scraped. Exiting pipeline run.")
        return

    # --- PHASE 2: Retrieve from SQS & Run ETL ---
    print("\n[STEP 2/4] Retrieving raw jobs from SQS and running ETL...")
    sqs = boto3.client("sqs", region_name=region)

    # Get Queue URL
    try:
        queue_url_response = sqs.get_queue_url(QueueName=settings.sqs_raw_jobs_queue)
        queue_url = queue_url_response["QueueUrl"]
    except Exception as e:
        print(f"Error getting SQS URL: {e}")
        return

    # Receive messages from SQS
    records = []

    print(f"Fetching messages from queue: {queue_url}...")
    while True:
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=5
        )

        messages = response.get("Messages", [])
        if not messages:
            break

        for msg in messages:
            records.append({
                "body": msg["Body"],
                "receiptHandle": msg["ReceiptHandle"]
            })

        print(f"Received {len(messages)} messages (total retrieved: {len(records)})")

        # SQS Receive Message limit is 10. We loop until the queue is empty.
        if len(messages) < 10:
            break

    if not records:
        print("No messages found in SQS queue.")
        return

    # Invoke ETL Handler with SQS event format
    print(f"Triggering ETL handler with {len(records)} SQS records...")
    event = {"Records": records}
    try:
        etl_summary = etl_handler(event, None)
        print(f"ETL Run Summary: {json.dumps(etl_summary, indent=2, ensure_ascii=False)}")

        # Delete successfully processed messages from SQS
        print("Cleaning up SQS queue...")
        for rec in records:
            sqs.delete_message(
                QueueUrl=queue_url,
                ReceiptHandle=rec["receiptHandle"]
            )
        print("SQS queue cleaned.")

    except Exception as e:
        print(f"ETL Execution failed: {e}")
        return

    # --- PHASE 3: Run Matcher & Send Telegram Alerts ---
    print("\n[STEP 3/4] Running Keyword Matcher and sending Telegram Alerts...")
    try:
        # Trigger matcher handler
        # It gets jobs scraped in last 6 hours, matches with DynamoDB subs, and notifies users
        matcher_summary = matcher_handler(None, None)
        print(f"Matcher Run Summary: {json.dumps(matcher_summary, indent=2, ensure_ascii=False)}")
    except Exception as e:
        print(f"Matcher Execution failed: {e}")

    print("\n" + "=" * 60)
    print("PIPELINE RUN COMPLETED SUCCESSFULLY!")
    print("=" * 60)

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="ViecLamBot end-to-end or external scrape run")
    parser.add_argument(
        "--sources", default="",
        help="comma-separated JobSource values (e.g. jobsgo,timviec365): scrape only these "
        "and push to SQS, skipping ETL/matcher. Default: the full local pipeline.",
    )
    parser.add_argument(
        "--keywords", default="",
        help="comma-separated keywords for --sources runs (default: the current seed group)",
    )
    parser.add_argument(
        "--latest-only", action="store_true",
        help="with --sources: only each board's newest-listings page, no keyword searches",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    run_pipeline(parse_args())
