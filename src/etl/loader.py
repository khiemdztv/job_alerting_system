"""
Data Loader for ViecLamBot.

Loads processed Job objects to:
1. DynamoDB (for queries and matching)
2. S3 (as Parquet files for data lake)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
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
    "first_seen_at",
    "posted_at",
    "expires_at",
    "ttl",
    "tags",
    "skills",
    "level",
    "job_type",
    "industry",
    "district",
    "experience_min_years",
    "canonical_group_id",
    "search_blob",
)


# _clean_vn_text moved to src.common.text_utils.clean_vn_text


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


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

            self._upsert_item(item, first_seen_at=datetime.now(timezone.utc).isoformat())
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
        now = datetime.now(timezone.utc)

        # Upsert item by item so a re-scraped posting keeps its original
        # ``first_seen_at``; a plain batch put would overwrite it with the
        # latest crawl time and make every old job look new again.
        for job in jobs:
            try:
                item = job.to_dynamo_item()
                item["ttl"] = int(now.timestamp()) + ttl_seconds
                self._upsert_item(item, first_seen_at=now.isoformat())
                loaded += 1
            except ClientError as e:
                logger.error(f"DynamoDB upsert failed for job '{job.title}': {e}")
                raise
            except Exception as e:
                logger.warning(f"Skipping job '{job.title}': {e}")

        logger.info(
            f"Loaded {loaded}/{len(jobs)} jobs to DynamoDB",
            extra={"job_count": loaded},
        )
        return loaded

    def _upsert_item(self, item: dict, *, first_seen_at: str) -> None:
        """SET every attribute, but only initialise ``first_seen_at`` once."""
        key = {"job_id": item["job_id"]}
        names, values, sets = {}, {}, []
        for index, (field, value) in enumerate(item.items()):
            if field == "job_id":
                continue
            names[f"#a{index}"] = field
            values[f":v{index}"] = value
            sets.append(f"#a{index} = :v{index}")
        names["#first_seen"] = "first_seen_at"
        values[":first_seen"] = first_seen_at
        sets.append("#first_seen = if_not_exists(#first_seen, :first_seen)")
        self.table.update_item(
            Key=key,
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

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
                    posted_since: datetime | None = None,
                    deadline_at: float | None = None,
                    min_score: int | None = None) -> list[dict]:
        """Rank jobs for ``keyword``.  ``min_score`` defaults to the alert floor
        (title must name the occupation); interactive search passes a lower one.

        The prebuilt search index (``src.search_index``) supplies a candidate set
        when it exists and is younger than ``VIECLAMBOT_SEARCH_INDEX_MAX_AGE_SECONDS``;
        otherwise, or on any index error, the full-table snapshot is ranked.
        ``rank_jobs`` is the final ranker on both paths.
        """
        from src.matcher.search import ALERT_MIN_SCORE, rank_jobs
        rank = {
            "location": location, "salary_min": salary_min, "since": since,
            "posted_since": posted_since, "limit": limit,
            "max_age_days": get_settings().max_job_age_days,
            "min_score": ALERT_MIN_SCORE if min_score is None else min_score,
        }
        started = time.perf_counter()
        candidates = self._index_candidates(keyword, deadline_at=deadline_at, **rank)
        if candidates is not None:
            results = rank_jobs(candidates, keyword, **rank)
            logger.info(
                "Search path=index candidates=%s results=%s in %.0f ms",
                len(candidates), len(results), (time.perf_counter() - started) * 1000,
            )
            return results
        # Storage failures must propagate; they are not an empty search result.
        results = rank_jobs(self.get_search_items(deadline_at=deadline_at), keyword, **rank)
        logger.info("Search path=scan results=%s in %.0f ms",
                    len(results), (time.perf_counter() - started) * 1000)
        return results

    # ── Search index path ────────────────────────────────────────────────────

    def _search_index(self):
        """The container-cached index, or None when absent (overridable in tests)."""
        from src.search_index import SearchIndex

        return SearchIndex.load(get_settings().s3_data_lake_bucket)

    def _index_candidates(self, keyword: str, *, location, salary_min, since, posted_since,
                          limit, max_age_days, min_score,
                          deadline_at: float | None = None) -> list[dict] | None:
        """Candidates from the search index, or None to fall back to the scan.

        Jobs loaded after the index build are only in DynamoDB.  Interactive
        ``/search`` accepts that lag (at most one indexer period plus
        ``..._MAX_AGE_SECONDS``).  The matcher (``since`` given) must not miss new
        jobs, so when the index is older than ``..._DELTA_AFTER_SECONDS`` it also
        reads the recent delta with a filtered Scan (see ``_index_delta_items``).
        """
        del min_score  # rank_jobs applies the score floor
        if not _env_bool("VIECLAMBOT_SEARCH_INDEX_ENABLED", True):
            return None
        try:
            index = self._search_index()
            if index is None:
                logger.info("Search index missing; using the table scan")
                return None
            age = index.age_seconds()
            if age > _env_int("VIECLAMBOT_SEARCH_INDEX_MAX_AGE_SECONDS", 3 * 3600):
                logger.warning("Search index is %.0f s old; using the table scan", age)
                return None
            from src.matcher.search import extract_salary_filter

            # The same salary bounds rank_jobs will apply, so a limit never cuts
            # jobs that would pass in favour of jobs it will reject.
            _, query_min, query_max = extract_salary_filter(keyword)
            candidate_limit = None
            if limit is not None:
                candidate_limit = max(
                    limit, _env_int("VIECLAMBOT_SEARCH_INDEX_CANDIDATE_LIMIT", 2000)
                )
            jobs = index.candidates(
                [keyword], location, limit=candidate_limit,
                salary_min=salary_min if salary_min is not None else query_min,
                salary_max=query_max, since=since, posted_since=posted_since,
                max_age_days=max_age_days,
            )
            if since is not None and age > _env_int(
                "VIECLAMBOT_SEARCH_INDEX_DELTA_AFTER_SECONDS", 20 * 60
            ):
                delta = self._index_delta_items(index.scan_started_at, deadline_at)
                if delta:
                    merged = {job.get("job_id"): job for job in jobs}
                    # The DynamoDB copy is newer than the indexed one.
                    merged.update((job.get("job_id"), job) for job in delta)
                    jobs = list(merged.values())
            return jobs
        except Exception:
            logger.warning("Search index failed; using the table scan", exc_info=True)
            return None

    def _index_delta_items(self, built_from: datetime | None,
                           deadline_at: float | None = None) -> list[dict]:
        """Items scraped (or re-scraped) since the index scan started.

        Trade-off: a filtered Scan still *reads* (and is billed for) every item;
        it only saves transfer, Python heap and ranking time.  It runs only for
        the matcher, a few times a day, and is cached like the snapshot.  A GSI on
        the scrape hour or an ETL-written delta object would make it cheap and is
        left for later.
        """
        if built_from is None:
            return []
        margin = _env_int("VIECLAMBOT_SEARCH_INDEX_DELTA_MARGIN_SECONDS", 300)
        threshold = (built_from - timedelta(seconds=margin)).astimezone(timezone.utc)
        cutoff = threshold.isoformat()
        cache = getattr(self, "_index_delta", None)
        cache_seconds = max(60, get_settings().search_snapshot_cache_seconds)
        if cache and cache[0] == cutoff and time.monotonic() - cache[1] <= cache_seconds:
            return cache[2]
        names = {f"#f{index}": field for index, field in enumerate(SEARCH_FIELDS)}
        names["#scraped"] = "scraped_at"
        kwargs = {
            "ProjectionExpression": ", ".join(n for n in names if n != "#scraped"),
            "ExpressionAttributeNames": names,
            "FilterExpression": "#scraped >= :cutoff",
            "ExpressionAttributeValues": {":cutoff": cutoff},
        }
        items: list[dict] = []
        while True:
            if deadline_at is not None and time.monotonic() >= deadline_at:
                logger.warning("Search delta scan stopped at the deadline")
                break
            response = self.table.scan(**kwargs)
            items.extend(response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        logger.info("Search delta since %s: %s jobs", cutoff, len(items))
        self._index_delta = (cutoff, time.monotonic(), items)
        return items



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
