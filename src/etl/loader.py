"""
Data Loader for ViecLamBot.

Loads processed Job objects to:
1. DynamoDB (for queries and matching)
2. S3 (as Parquet files for data lake)
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from src.common.logger import get_logger
from src.common.models import Job
from src.config import get_settings

logger = get_logger(__name__)

SEARCH_FIELDS = (
    "job_id",
    "title",
    "title_normalized",
    "company",
    "location",
    "location_normalized",
    "salary_raw",
    "salary_min",
    "salary_max",
    "source",
    "source_url",
    "scraped_at",
    "posted_at",
    "expires_at",
    "ttl",
    "tags",
    "search_blob",
)


# _clean_vn_text moved to src.common.text_utils.clean_vn_text


class DynamoDBLoader:
    """Load jobs into DynamoDB."""

    def __init__(self, table_name: Optional[str] = None):
        settings = get_settings()
        self.table_name = table_name or settings.dynamodb_jobs_table
        self.region = settings.aws_region
        self._table = None

    @property
    def table(self):
        if self._table is None:
            dynamodb = boto3.resource("dynamodb", region_name=self.region)
            self._table = dynamodb.Table(self.table_name)
        return self._table

    def load_job(self, job: Job) -> bool:
        """Load a single job into DynamoDB.

        Args:
            job: Processed job object.

        Returns:
            True if loaded successfully.
        """
        try:
            item = job.to_dynamo_item()

            # Add TTL (auto-expire after max_job_age_days)
            settings = get_settings()
            ttl_seconds = settings.max_job_age_days * 24 * 3600
            item["ttl"] = int(datetime.now(timezone.utc).timestamp()) + ttl_seconds

            self.table.put_item(Item=item)
            self.__dict__.pop("_search_items", None)
            self.__dict__.pop("_search_items_loaded_at", None)
            self.__dict__.pop("_search_items_complete", None)
            return True

        except ClientError as e:
            logger.error(f"DynamoDB put failed for job '{job.title}': {e}")
            return False

    def load_batch(self, jobs: list[Job]) -> int:
        """Batch load jobs into DynamoDB.

        Uses batch_writer for efficient writes (auto-batches into groups of 25).

        Args:
            jobs: List of processed jobs.

        Returns:
            Number of successfully loaded jobs.
        """
        settings = get_settings()
        ttl_seconds = settings.max_job_age_days * 24 * 3600
        self.__dict__.pop("_search_items", None)
        self.__dict__.pop("_search_items_loaded_at", None)
        self.__dict__.pop("_search_items_complete", None)
        loaded = 0

        try:
            with self.table.batch_writer(overwrite_by_pkeys=["job_id"]) as batch:
                for job in jobs:
                    try:
                        item = job.to_dynamo_item()
                        item["ttl"] = int(datetime.now(timezone.utc).timestamp()) + ttl_seconds
                        batch.put_item(Item=item)
                        loaded += 1
                    except Exception as e:
                        logger.warning(f"Skipping job '{job.title}': {e}")

            logger.info(
                f"Loaded {loaded}/{len(jobs)} jobs to DynamoDB",
                extra={"job_count": loaded},
            )

        except ClientError as e:
            logger.error(f"DynamoDB batch write failed: {e}")
            raise

        return loaded

    def get_existing_ids(self, source: Optional[str] = None) -> set[str]:
        """Get existing job IDs from DynamoDB for deduplication.

        Args:
            source: Optional source filter.

        Returns:
            Set of existing job_id strings.
        """
        existing_ids: set[str] = set()

        try:
            scan_kwargs = {
                "ProjectionExpression": "job_id",
            }

            if source:
                scan_kwargs["FilterExpression"] = "#src = :src"
                scan_kwargs["ExpressionAttributeNames"] = {"#src": "source"}
                scan_kwargs["ExpressionAttributeValues"] = {":src": source}

            response = self.table.scan(**scan_kwargs)
            for item in response.get("Items", []):
                existing_ids.add(item["job_id"])

            # Handle pagination
            while "LastEvaluatedKey" in response:
                scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self.table.scan(**scan_kwargs)
                for item in response.get("Items", []):
                    existing_ids.add(item["job_id"])

            logger.info(f"Found {len(existing_ids)} existing job IDs in DynamoDB")

        except ClientError as e:
            logger.error(f"DynamoDB scan failed: {e}")

        return existing_ids

    def get_search_items(self, deadline_at: float | None = None) -> list[dict]:
        """Read scan pages with a short warm-container cache and deadline guard."""
        settings = get_settings()
        loaded_at = float(getattr(self, "_search_items_loaded_at", 0))
        cached_complete = bool(getattr(self, "_search_items_complete", False))
        cache_seconds = settings.search_snapshot_cache_seconds
        if not cached_complete:
            cache_seconds = min(60, cache_seconds)
        if (
            hasattr(self, "_search_items")
            and time.monotonic() - loaded_at <= cache_seconds
        ):
            self._last_search_complete = cached_complete
            return self._search_items
        items = []
        field_names = {f"#f{index}": field for index, field in enumerate(SEARCH_FIELDS)}
        kwargs = {
            "ProjectionExpression": ", ".join(field_names),
            "ExpressionAttributeNames": field_names,
        }
        complete = True
        while True:
            if deadline_at is not None and time.monotonic() >= deadline_at:
                complete = False
                break
            response = self.table.scan(**kwargs)
            items.extend(response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        if not items and not complete:
            raise TimeoutError("DynamoDB search deadline reached before the first scan page")
        self._last_search_complete = complete
        self._search_items = items
        self._search_items_loaded_at = time.monotonic()
        self._search_items_complete = complete
        if not complete:
            logger.warning(
                "Search deadline reached; ranking a partial snapshot of %s jobs", len(items)
            )
        logger.info("Search snapshot loaded: %s jobs (complete=%s)", len(items), complete)
        return items

    def search_jobs(self, keyword: str, limit: int | None = 20, *,
                    location: str | None = None, salary_min: int | None = None,
                    since: datetime | None = None,
                    deadline_at: float | None = None) -> list[dict]:
        from src.matcher.search import rank_jobs
        # Storage failures must propagate; they are not an empty search result.
        return rank_jobs(self.get_search_items(deadline_at=deadline_at), keyword,
                         location=location,
                         salary_min=salary_min, since=since, limit=limit,
                         max_age_days=get_settings().max_job_age_days)



class S3Loader:
    """Load raw and processed data to S3 data lake."""

    def __init__(self, bucket_name: Optional[str] = None):
        settings = get_settings()
        self.bucket_name = bucket_name or settings.s3_data_lake_bucket
        self.region = settings.aws_region
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    def save_raw_data(self, data: list[dict], source: str) -> str:
        """Save raw scraped data to S3.

        Args:
            data: List of raw job dicts.
            source: Source name (e.g., "jooble", "careerlink").

        Returns:
            S3 key of saved file.
        """
        now = datetime.now(timezone.utc)
        key = (
            f"raw/{source}/{now.strftime('%Y/%m/%d')}/"
            f"{source}_{now.strftime('%Y%m%d_%H%M%S')}.json"
        )

        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=json.dumps(data, ensure_ascii=False, default=str),
                ContentType="application/json",
            )
            logger.info(f"Saved raw data to s3://{self.bucket_name}/{key}")
            return key

        except ClientError as e:
            logger.error(f"S3 upload failed: {e}")
            return ""

    def save_processed_data(self, jobs: list[Job]) -> str:
        """Save processed jobs as JSON to S3.

        Args:
            jobs: List of processed Job objects.

        Returns:
            S3 key of saved file.
        """
        now = datetime.now(timezone.utc)
        key = (
            f"processed/{now.strftime('%Y/%m/%d')}/"
            f"jobs_processed_{now.strftime('%Y%m%d_%H%M%S')}.json"
        )

        try:
            data = [job.model_dump(mode="json") for job in jobs]
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=json.dumps(data, ensure_ascii=False, default=str),
                ContentType="application/json",
            )
            logger.info(f"Saved {len(jobs)} processed jobs to s3://{self.bucket_name}/{key}")
            return key

        except ClientError as e:
            logger.error(f"S3 upload failed: {e}")
            return ""
