"""
Data Loader for ViecLamBot.

Loads processed Job objects to:
1. DynamoDB (for queries and matching)
2. S3 (as Parquet files for data lake)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from src.common.logger import get_logger
from src.common.models import Job
from src.config import get_settings

logger = get_logger(__name__)


def _clean_vn_text(text: str) -> str:
    """Helper to lowercase, normalize to NFC, strip Vietnamese accents, and replace y with i."""
    if not text:
        return ""
    import unicodedata
    
    # Standardize to NFC and lowercase
    text = unicodedata.normalize("NFC", text.lower())
    
    # Map of accented characters to base letters
    accent_map = {
        'a': 'aàáảãạăằắẳẵặâầấẩẫậ',
        'e': 'eèéẻẽẹêềếểễệ',
        'i': 'iìíỉĩị',
        'o': 'oòóỏõọôồốổỗộơờớởỡợ',
        'u': 'uùúủũụưừứửữự',
        'y': 'yỳýỷỹỵ',
        'd': 'dđ',
    }
    
    char_map = {}
    for base, chars in accent_map.items():
        for char in chars:
            char_map[char] = base
            
    result = []
    for char in text:
        result.append(char_map.get(char, char))
        
    cleaned = "".join(result)
    # Treat 'y' and 'i' as equivalent (e.g. kĩ vs kỹ, công nghệ thông tin vs công nghệ thông tyn)
    cleaned = cleaned.replace("y", "i")
    return cleaned


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
        loaded = 0

        try:
            with self.table.batch_writer() as batch:
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

    def search_jobs(self, keyword: str, limit: int = 20) -> list[dict]:
        """Search jobs by keyword with smart Python-based token matching.

        For production, consider using DynamoDB with OpenSearch for full-text search.

        Args:
            keyword: Search keyword.
            limit: Max results to return.

        Returns:
            List of matching job items.
        """
        try:
            import re

            # Scan the table to fetch all items for client-side search.
            # Safe and fast for small-to-medium tables (under 10,000 items).
            projection = "job_id, title, title_normalized, company, #loc, location_normalized, salary_raw, tags, #src, source_url, posted_at, scraped_at"
            scan_kwargs = {
                "ProjectionExpression": projection,
                "ExpressionAttributeNames": {
                    "#loc": "location",
                    "#src": "source"
                }
            }
            
            response = self.table.scan(**scan_kwargs)
            items = response.get("Items", [])
            
            while "LastEvaluatedKey" in response:
                scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self.table.scan(**scan_kwargs)
                items.extend(response.get("Items", []))

            # Clean and split keyword into search tokens
            keyword_clean_vn = _clean_vn_text(keyword)
            raw_tokens = re.split(r"[,\s\-/|]+", keyword_clean_vn)
            search_tokens = [t.strip() for t in raw_tokens if len(t.strip()) > 1]
            
            # Fallback if no valid tokens extracted
            if not search_tokens:
                search_tokens = [keyword_clean_vn]

            matched_items = []
            for item in items:
                title_norm = item.get("title_normalized", "").lower()
                title_clean = _clean_vn_text(title_norm)
                
                tags = [t.lower() for t in item.get("tags", [])]
                tags_clean = [_clean_vn_text(t) for t in tags]
                
                # We want to match all search tokens (AND behavior)
                is_match = True
                for token in search_tokens:
                    token_in_title = token in title_clean
                    token_in_tags = any(token in tag for tag in tags_clean)
                    
                    # Synonym mappings for intern/fresher
                    if token in ["intern", "thuc tap", "tts"]:
                        token_in_title = (
                            token_in_title 
                            or "intern" in title_clean 
                            or "thuc tap" in title_clean 
                            or "tts" in title_clean
                        )
                    if token in ["fresher", "moi tot nghiep"]:
                        token_in_title = (
                            token_in_title 
                            or "fresher" in title_clean 
                            or "moi tot nghiep" in title_clean
                        )
                        
                    if not (token_in_title or token_in_tags):
                        is_match = False
                        break
                        
                if is_match:
                    matched_items.append(item)

            # Sort by posted_at descending (fallback to scraped_at, newest first)
            matched_items.sort(key=lambda x: x.get("posted_at") or x.get("scraped_at", ""), reverse=True)

            return matched_items[:limit]

        except ClientError as e:
            logger.error(f"DynamoDB scan for search failed: {e}")
            return []
        except Exception as e:
            logger.error(f"Python-based smart search failed: {e}", exc_info=True)
            return []



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
