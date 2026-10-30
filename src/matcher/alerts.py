"""Recoverable alert delivery using per-user leases and a persistent sent ledger.

Per-user preferences (``PREF``) decide *when* a user gets a digest (frequency,
quiet hours — see :mod:`src.bot.schedule`) and how many jobs; ``HIDDEN#``,
``EXCLUDE#`` and the level/work-type filters decide *which* jobs; ``FOLLOW#``
adds new postings from followed companies.  Undelivered jobs stay pending, so a
skipped or deferred run never drops anything.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from src.bot.filters import apply_context, company_matches
from src.bot.handler import TelegramBot
from src.bot.keyboards import job_buttons
from src.bot.messages import job_pages
from src.bot.schedule import LOOKBACK_DAYS, is_due
from src.bot.store import UserStore, compact_job
from src.common.logger import get_logger
from src.config import get_settings
from src.etl.loader import DynamoDBLoader
from src.matcher.keyword_matcher import KeywordMatcher
from src.matcher.search import ALERT_MIN_SCORE, identity, matches_filters

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
                        # Small card payload so /history and 💾 work without the jobs table.
                        **compact_job(job),
                    }
                )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _alert_groups(subs, ctx, loader, settings, since, seen) -> list[tuple[str, list[dict]]]:
    """[(heading, pending jobs)] per subscription and followed company.

    Each job appears in at most one group and never if already sent, hidden,
    blacklisted or outside the user's level/work-type filters.  Salary and
    location stay per subscription.
    """
    raw_groups = []
    for sub in subs:
        matched = loader.search_jobs(
            sub.keyword_normalized,
            limit=None,
            location=sub.location_filter,
            salary_min=sub.salary_min_filter,
            since=since,
            # Alerts only send postings whose title names the occupation.
            min_score=ALERT_MIN_SCORE,
        )
        raw_groups.append((f"🔔 Việc mới: {sub.keyword_raw}", matched))
    if ctx.follows:
        items = loader.get_search_items()
        for term, name in ctx.follows.items():
            matched = [
                job for job in items
                if company_matches(job, term)
                and matches_filters(job, since=since, max_age_days=settings.max_job_age_days)
            ]
            raw_groups.append((f"🏢 Việc mới từ {name}", matched))
    claimed, groups = set(seen), []
    for heading, jobs in raw_groups:
        pending = []
        for job in apply_context(jobs, ctx, use_salary=False):
            key = identity(job)
            if key not in claimed:
                claimed.add(key)
                pending.append(job)
        groups.append((heading, pending))
    return groups


def run_alerts(event, context):
    settings = get_settings()
    dry_run = event.get("dry_run", False) is True
    now = _utcnow()
    subscriptions, follows = KeywordMatcher().load_alert_targets(settings.dynamodb_users_table)
    loader = DynamoDBLoader()
    store = DeliveryStore(settings)
    user_store = UserStore(table=store.table, settings=settings)
    bot = TelegramBot()
    summary = {
        "status": "dry_run" if dry_run else "success",
        "users_with_subs": len(subscriptions),
        "users_with_follows": len(follows),
        "total_matches": 0,
        "notifications_sent": 0,
        "send_failures": 0,
        "deferred_users": 0,
        "deferred_jobs": 0,
        "scheduled_skips": 0,
    }
    owner = str(uuid.uuid4())
    for user_id in dict.fromkeys([*subscriptions, *follows]):
        subs = subscriptions.get(user_id, [])
        if context and context.get_remaining_time_in_millis() < 20000:
            summary["deferred_users"] += 1
            continue
        acquired = False
        try:
            ctx = user_store.load_context(user_id)
            frequency = str(ctx.prefs.get("frequency") or "instant")
            if not is_due(ctx.prefs, now, settings.alert_interval_hours):
                # Frequency slot not reached or quiet hours: jobs stay pending.
                summary["scheduled_skips"] += 1
                continue
            if not dry_run:
                acquired = store.acquire(user_id, owner)
                if not acquired:
                    summary["deferred_users"] += 1
                    continue
            seen = store.sent_ids(user_id)
            lookback = max(settings.alert_recovery_days, LOOKBACK_DAYS.get(frequency, 0))
            since = now - timedelta(days=lookback)
            groups = _alert_groups(subs, ctx, loader, settings, since, seen)
            total = sum(len(pending) for _, pending in groups)
            summary["total_matches"] += total
            if dry_run:
                continue
            remaining = int(ctx.prefs.get("max_jobs") or 0) or settings.alert_max_jobs_per_user
            quota = max(1, remaining // max(1, len(groups)))
            selections = []
            for heading, pending in groups:
                selected = pending[: min(quota, remaining)]
                summary["deferred_jobs"] += len(pending) - len(selected)
                remaining -= len(selected)
                if selected:
                    selections.append((heading, selected))
            header = ""
            if selections:
                period = "Tuần này" if frequency == "weekly" else "Hôm nay"
                with_jobs = sum(1 for _, pending in groups if pending)
                header = f"📬 <b>{period} có {total} tin mới cho {with_jobs} đăng ký</b>\n\n"
            for heading, selected in selections:
                for message, included in job_pages(selected, heading):
                    if context and context.get_remaining_time_in_millis() < 20000:
                        raise TimeoutError("Alert delivery deferred before Lambda timeout")
                    if header:
                        message, header = header + message, ""
                    if not bot.send_message(user_id, message, parse_mode="HTML",
                                            reply_markup=job_buttons(included)):
                        raise RuntimeError("Telegram did not confirm alert delivery")
                    # Only acknowledge exactly the jobs Telegram confirmed receiving.
                    store.mark_sent(user_id, included)
                    seen.update(identity(job) for job in included)
                    summary["notifications_sent"] += 1
            if frequency != "instant":
                user_store.set_pref(user_id, last_alert_at=now.isoformat())
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


