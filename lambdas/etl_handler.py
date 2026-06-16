"""
Lambda Handler: ETL Processor

Triggered by SQS messages containing raw job data.
Transforms, deduplicates, and loads into DynamoDB + S3.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.logger import get_logger
from src.common.models import RawJob
from src.config import get_settings
from src.etl.deduplicator import Deduplicator
from src.etl.loader import DynamoDBLoader, S3Loader
from src.etl.transformer import Transformer

logger = get_logger(__name__)


def handler(event, context):
    """Lambda handler for ETL processing.

    Triggered by SQS with raw job data.

    Args:
        event: SQS event with Records.
        context: Lambda context.

    Returns:
        Processing summary.
    """
    settings = get_settings()
    start_time = datetime.now(timezone.utc)

    logger.info("Starting ETL processing")

    # Parse SQS records
    raw_jobs: list[RawJob] = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            raw_job = RawJob(**body)
            raw_jobs.append(raw_job)
        except Exception as e:
            logger.warning(f"Failed to parse SQS record: {e}")

    if not raw_jobs:
        logger.info("No valid raw jobs to process")
        return {"status": "empty", "processed": 0}

    # Transform
    transformer = Transformer()
    jobs = transformer.transform_batch(raw_jobs)

    # Deduplicate within batch
    deduplicator = Deduplicator()
    jobs = deduplicator.deduplicate(jobs, cross_source=True)

    # Deduplicate against existing DB
    db_loader = DynamoDBLoader()
    existing_ids = db_loader.get_existing_ids()
    jobs = deduplicator.deduplicate_against_existing(jobs, existing_ids)

    # Load to DynamoDB
    loaded_count = 0
    if jobs:
        loaded_count = db_loader.load_batch(jobs)

    # Save processed data to S3
    s3_loader = S3Loader()
    if jobs:
        s3_loader.save_processed_data(jobs)

    duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)

    summary = {
        "status": "success",
        "raw_input": len(raw_jobs),
        "transformed": len(jobs),
        "loaded_to_db": loaded_count,
        "duration_ms": duration_ms,
    }

    logger.info(
        f"ETL complete: {len(raw_jobs)} raw → {len(jobs)} processed → {loaded_count} loaded",
        extra={"job_count": loaded_count, "duration_ms": duration_ms},
    )

    return summary
