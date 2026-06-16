"""
Lambda Handler: Matcher & Notification

Triggered by EventBridge every 6 hours (after scraper completes).
Matches new jobs to user subscriptions and sends Telegram alerts.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.bot.handler import TelegramBot
from src.common.logger import get_logger
from src.config import get_settings
from src.matcher.keyword_matcher import KeywordMatcher, format_notification

logger = get_logger(__name__)


def handler(event, context):
    """Lambda handler for matching and notifications.

    Runs after scraper, matches new jobs to subscriptions, sends Telegram alerts.

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

    # Get jobs scraped in the last 6 hours
    since = datetime.now(timezone.utc) - timedelta(hours=settings.alert_interval_hours)
    new_jobs = matcher.get_new_jobs_since(settings.dynamodb_jobs_table, since)

    if not new_jobs:
        logger.info("No new jobs to match")
        return {"status": "no_new_jobs", "notifications_sent": 0}

    # Get all active subscriptions
    user_subs = matcher.get_all_active_subscriptions(settings.dynamodb_users_table)

    if not user_subs:
        logger.info("No active subscriptions")
        return {"status": "no_subscriptions", "notifications_sent": 0}

    # Match and notify
    notifications_sent = 0
    total_matches = 0

    for user_id, subscriptions in user_subs.items():
        user_matches: dict[str, list[dict]] = {}  # keyword -> matching jobs

        for sub in subscriptions:
            matched_jobs = matcher.match_jobs_to_subscription(new_jobs, sub)
            if matched_jobs:
                user_matches[sub.keyword_raw] = matched_jobs
                total_matches += len(matched_jobs)

        # Send notification for this user (one message per keyword)
        if user_matches:
            for keyword, jobs in user_matches.items():
                message = format_notification(keyword, jobs)
                if message:
                    success = bot.send_message(user_id, message)
                    if success:
                        notifications_sent += 1

    duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)

    summary = {
        "status": "success",
        "new_jobs": len(new_jobs),
        "users_with_subs": len(user_subs),
        "total_matches": total_matches,
        "notifications_sent": notifications_sent,
        "duration_ms": duration_ms,
    }

    logger.info(
        f"Matcher complete: {len(new_jobs)} new jobs, {total_matches} matches, "
        f"{notifications_sent} notifications sent",
        extra={"duration_ms": duration_ms},
    )

    return summary
