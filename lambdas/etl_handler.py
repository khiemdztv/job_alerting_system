"""
Lambda Handler: ETL Processor

Triggered by SQS messages containing raw job data.
Transforms (with v2 enrichment), fills thin postings from their detail page
within a small budget, deduplicates, and loads into DynamoDB + S3 (JSON and
gzip NDJSON for Athena).
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from botocore.exceptions import BotoCoreError, ClientError

from src.common.logger import get_logger
from src.common.models import Job, RawJob
from src.etl.deduplicator import Deduplicator
from src.etl.detail_fetch import DetailFetcher, fill_details
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
            raise ValueError("Invalid SQS record; retain the batch for retry/DLQ") from e

    if not raw_jobs:
        logger.info("No valid raw jobs to process")
        return {"status": "empty", "processed": 0}

    # Transform
    transformer = Transformer()
    jobs = transformer.transform_batch(raw_jobs)
    if len(jobs) != len(raw_jobs):
        raise RuntimeError("ETL transformation incomplete; retain batch for retry")

    # Fill thin postings (short description / no deadline) from their detail page
    db_loader = DynamoDBLoader()
    jobs, detail_stats = _fill_details(raw_jobs, jobs, transformer, db_loader, context)

    # Deduplicate within batch
    deduplicator = Deduplicator()
    jobs = deduplicator.deduplicate(jobs, cross_source=False)

    # Load to DynamoDB (insert new or overwrite existing jobs with fresh scraped_at)
    loaded_count = 0
    if jobs:
        loaded_count = db_loader.load_batch(jobs)
        if loaded_count != len(jobs):
            raise RuntimeError("ETL write incomplete; retain batch for retry")

    # Save processed data to S3
    s3_loader = S3Loader()
    if jobs:
        s3_loader.save_processed_data(jobs)
        if _env_flag("VIECLAMBOT_PROCESSED_NDJSON_ENABLED", True):
            save_processed_ndjson(jobs, s3_loader)

    duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)

    summary = {
        "status": "success",
        "raw_input": len(raw_jobs),
        "transformed": len(jobs),
        "loaded_to_db": loaded_count,
        "details": detail_stats,
        "duration_ms": duration_ms,
    }

    logger.info(
        f"ETL complete: {len(raw_jobs)} raw → {len(jobs)} processed → {loaded_count} loaded",
        extra={"job_count": loaded_count, "duration_ms": duration_ms},
    )

    return summary


# ── Detail fetch ────────────────────────────────────────────────

_STORED_DETAIL_FIELDS = (
    "job_id, description, requirements, expires_at, benefits, details_fetched_at"
)


def _fill_details(
    raw_jobs: list[RawJob],
    jobs: list[Job],
    transformer: Transformer,
    db_loader: DynamoDBLoader,
    context,
) -> tuple[list[Job], dict]:
    """Re-transform postings improved by a detail page (or by details stored earlier)."""
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    fetcher = DetailFetcher(remaining_ms=remaining if callable(remaining) else None)
    if not fetcher.config.enabled or fetcher.config.max_pages <= 0:
        return jobs, {"enabled": False}

    def lookup(job_id: str):
        try:
            response = db_loader.table.get_item(
                Key={"job_id": job_id}, ProjectionExpression=_STORED_DETAIL_FIELDS
            )
        except (ClientError, BotoCoreError) as exc:
            logger.info(f"Stored detail lookup failed for {job_id}: {exc}")
            return None
        return response.get("Item")

    filled, info, stats = fill_details(
        raw_jobs, [job.job_id for job in jobs], fetcher, lookup=lookup
    )
    result = list(jobs)
    for index, extra in info.items():
        job = transformer.transform(filled[index]) or result[index]
        fetched_at = extra.get("details_fetched_at")
        if fetched_at:
            job.details_fetched_at = datetime.fromisoformat(str(fetched_at))
        for code in extra.get("benefits") or ():
            if code not in job.benefits:
                job.benefits.append(code)
        result[index] = job
    if stats["candidates"]:
        logger.info(f"Detail fetch: {stats}")
    return result, stats


# ── Analytics export ────────────────────────────────────────────


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def processed_ndjson_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (f"processed/ndjson/{now.strftime('%Y/%m/%d')}/"
            f"jobs_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.ndjson.gz")


def processed_ndjson_body(jobs: list[Job]) -> bytes:
    """Gzip newline-delimited JSON, one job per line (Athena / OpenX JSON SerDe)."""
    lines = (
        json.dumps(job.model_dump(mode="json", exclude={"search_blob"}),
                   ensure_ascii=False, default=str)
        for job in jobs
    )
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))


def save_processed_ndjson(jobs: list[Job], s3_loader: S3Loader) -> str:
    """Write ``processed/ndjson/YYYY/MM/DD/*.ndjson.gz``; "" when S3 fails (non-fatal)."""
    key = processed_ndjson_key()
    try:
        s3_loader.client.put_object(
            Bucket=s3_loader.bucket_name,
            Key=key,
            Body=processed_ndjson_body(jobs),
            ContentType="application/gzip",
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error(f"S3 NDJSON upload failed: {exc}")
        return ""
    logger.info(f"Saved {len(jobs)} jobs to s3://{s3_loader.bucket_name}/{key}")
    return key
