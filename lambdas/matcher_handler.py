"""
Lambda Handler: Matcher & Notification

Triggered by EventBridge every 6 hours (after scraper completes).
Matches jobs in DB against user subscriptions and sends Telegram alerts.

Uses the SAME search logic as /search command (db_loader.search_jobs)
to ensure consistent results between search and subscription alerts.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.bot.handler import TelegramBot
from src.common.logger import get_logger
from src.config import get_settings
from src.etl.loader import DynamoDBLoader
from src.matcher.keyword_matcher import KeywordMatcher, format_notification

logger = get_logger(__name__)


def handler(event, context):
    """Lambda handler for matching and notifications.

    Uses db_loader.search_jobs() — the same matching logic as the /search
    command — so subscription alerts and manual searches always agree.

    Args:
        event: EventBridge scheduled event.
        context: Lambda context.

    Returns:
        Notification summary.
    """
    settings = get_settings()
    start_time = datetime.now(timezone.utc)

    logger.info("Starting matcher & notification run")

    matcher = KeywordMatcher()
    bot = TelegramBot()
    db_loader = DynamoDBLoader()

    # Get all active subscriptions
    user_subs = matcher.get_all_active_subscriptions(settings.dynamodb_users_table)

    if not user_subs:
        logger.info("No active subscriptions")
        return {"status": "no_subscriptions", "notifications_sent": 0}

    # Match and notify
    notifications_sent = 0
    total_matches = 0
    send_failures = 0

    for user_id, subscriptions in user_subs.items():
        user_matches: dict[str, list[dict]] = {}  # keyword -> matching jobs

        for sub in subscriptions:
            # Use db_loader.search_jobs — same logic as /search command
            matched_jobs = db_loader.search_jobs(sub.keyword_normalized, limit=50)

            # Apply location filter if set on the subscription
            if sub.location_filter:
                loc_filter = sub.location_filter.lower()
                matched_jobs = [
                    j for j in matched_jobs
                    if loc_filter in j.get("location_normalized", "").lower()
                    or loc_filter in j.get("location", "").lower()
                ]

            if matched_jobs:
                user_matches[sub.keyword_raw] = matched_jobs
                total_matches += len(matched_jobs)
                logger.info(
                    f"User {user_id}: keyword '{sub.keyword_raw}' matched {len(matched_jobs)} jobs"
                )
            else:
                logger.info(
                    f"User {user_id}: keyword '{sub.keyword_raw}' matched 0 jobs"
                )

        # Send notification for this user (one message per keyword)
        if user_matches:
            for keyword, jobs in user_matches.items():
                message = format_notification(keyword, jobs)
                if message:
                    logger.info(
                        f"Sending alert to user {user_id} for '{keyword}' ({len(jobs)} jobs, msg len={len(message)})"
                    )
                    success = bot.send_message(user_id, message)
                    if success:
                        notifications_sent += 1
                        logger.info(f"Alert sent successfully to user {user_id} for '{keyword}'")
                    else:
                        send_failures += 1
                        logger.error(
                            f"Failed to send alert to user {user_id} for '{keyword}' (msg len={len(message)})"
                        )
                else:
                    logger.warning(f"format_notification returned empty for keyword '{keyword}'")

    duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)

    summary = {
        "status": "success",
        "users_with_subs": len(user_subs),
        "total_matches": total_matches,
        "notifications_sent": notifications_sent,
        "send_failures": send_failures,
        "duration_ms": duration_ms,
    }

    logger.info(
        f"Matcher complete: {total_matches} matches, "
        f"{notifications_sent} notifications sent, {send_failures} failures",
        extra={"duration_ms": duration_ms},
    )

    return summary

