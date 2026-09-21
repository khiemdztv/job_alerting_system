"""Recoverable alert delivery using per-user leases and a persistent sent ledger."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from src.bot.handler import TelegramBot
from src.bot.messages import job_pages
from src.common.logger import get_logger
from src.config import get_settings
from src.etl.loader import DynamoDBLoader
from src.matcher.keyword_matcher import KeywordMatcher
from src.matcher.search import identity

logger = get_logger(__name__)


class DeliveryStore:
    def __init__(self, settings):
        self.table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
            settings.dynamodb_users_table
        )
        self.retention = settings.max_job_age_days * 86400

    def acquire(self, user_id: str, owner: str) -> bool:
        try:
            self.table.put_item(
                Item={
                    "user_id": user_id,
                    "sk": "ALERT_LOCK",
                    "owner": owner,
                    "lease_until": int(time.time()) + 960,
                },
                ConditionExpression="attribute_not_exists(user_id) OR lease_until < :now",
                ExpressionAttributeValues={":now": int(time.time())},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release(self, user_id: str, owner: str):
        self.table.delete_item(
            Key={"user_id": user_id, "sk": "ALERT_LOCK"},
            ConditionExpression="#owner = :owner",
            ExpressionAttributeNames={"#owner": "owner"},
            ExpressionAttributeValues={":owner": owner},
        )

    def sent_ids(self, user_id: str) -> set[str]:
        sent = set()
        kwargs = {
            "KeyConditionExpression": "user_id = :uid AND begins_with(sk, :prefix)",
            "ExpressionAttributeValues": {":uid": user_id, ":prefix": "SENT#"},
            "ConsistentRead": True,
        }
        while True:
            response = self.table.query(**kwargs)
            for item in response.get("Items", []):
                if int(item.get("ttl", 0)) > time.time():
                    sent.add(item["sk"][5:])
            if not response.get("LastEvaluatedKey"):
                return sent
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def mark_sent(self, user_id: str, jobs: list[dict]):
        with self.table.batch_writer() as batch:
            for job in jobs:
                batch.put_item(
                    Item={
                        "user_id": user_id,
                        "sk": "SENT#" + identity(job),
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                        "ttl": int(time.time()) + self.retention,
                    }
                )


def run_alerts(event, context):
    settings = get_settings()
    dry_run = event.get("dry_run", False) is True
    subscriptions = KeywordMatcher().get_all_active_subscriptions(settings.dynamodb_users_table)
    loader = DynamoDBLoader()
    store = DeliveryStore(settings)
    bot = TelegramBot()
    since = datetime.now(timezone.utc) - timedelta(days=settings.alert_recovery_days)
    summary = {
        "status": "dry_run" if dry_run else "success",
        "users_with_subs": len(subscriptions),
        "total_matches": 0,
        "notifications_sent": 0,
        "send_failures": 0,
        "deferred_users": 0,
        "deferred_jobs": 0,
    }
    owner = str(uuid.uuid4())
    for user_id, subs in subscriptions.items():
        if context and context.get_remaining_time_in_millis() < 20000:
            summary["deferred_users"] += 1
            continue
        acquired = False
        try:
            if not dry_run:
                acquired = store.acquire(user_id, owner)
                if not acquired:
                    summary["deferred_users"] += 1
                    continue
            seen = store.sent_ids(user_id)
            remaining = settings.alert_max_jobs_per_user
            quota = max(1, remaining // len(subs))
            for sub in subs:
                matched = loader.search_jobs(
                    sub.keyword_normalized,
                    limit=None,
                    location=sub.location_filter,
                    salary_min=sub.salary_min_filter,
                    since=since,
                )
                pending = [job for job in matched if identity(job) not in seen]
                summary["total_matches"] += len(pending)
                if dry_run:
                    seen.update(identity(job) for job in pending)
                    continue
                selected = pending[: min(quota, remaining)]
                summary["deferred_jobs"] += len(pending) - len(selected)
                remaining -= len(selected)
                for message, included in job_pages(selected, f"🔔 Việc mới: {sub.keyword_raw}"):
                    if context and context.get_remaining_time_in_millis() < 20000:
                        raise TimeoutError("Alert delivery deferred before Lambda timeout")
                    if not bot.send_message(user_id, message, parse_mode="HTML"):
                        raise RuntimeError("Telegram did not confirm alert delivery")
                    # Only acknowledge exactly the jobs Telegram confirmed receiving.
                    store.mark_sent(user_id, included)
                    seen.update(identity(job) for job in included)
                    summary["notifications_sent"] += 1
        except Exception:
            summary["send_failures"] += 1
            logger.exception("Alert delivery failed for one user; continuing others")
        finally:
            if acquired:
                try:
                    store.release(user_id, owner)
                except Exception:
                    summary["send_failures"] += 1
                    logger.exception("Could not release alert lease")
    logger.info("Matcher complete: %s", summary)
    if summary["send_failures"] or summary["deferred_users"]:
        # EventBridge/Lambda retries are useful only if the invocation fails.
        raise RuntimeError(f"Alert run incomplete: {summary}")
    return summary
