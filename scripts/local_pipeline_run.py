"""
Run the entire end-to-end Data Engineering pipeline locally using real AWS resources.
1. Runs live scrapers (CareerLink & ViecLam24h) for active user subscription keywords + defaults.
2. Validates raw jobs and pushes them to SQS.
3. Pulls jobs from SQS, transforms, deduplicates, and loads them to DynamoDB & S3.
4. Matches the newly scraped jobs against active subscriptions.
5. Sends real-time alerts to subscribed users via the Telegram Bot API.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
import boto3

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lambdas.scraper_handler import handler as scraper_handler
from lambdas.etl_handler import handler as etl_handler
from lambdas.matcher_handler import handler as matcher_handler
from src.config import get_settings

def run_pipeline():
    settings = get_settings()
    region = settings.aws_region
    
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
    messages_processed = 0
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

if __name__ == "__main__":
    run_pipeline()
