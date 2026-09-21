"""
Keyword Matcher for ViecLamBot.

Matches new jobs against user subscriptions and sends Telegram notifications.

Matching logic:
1. Load all active subscriptions
2. For each subscription keyword, find jobs where:
   - title_normalized contains the keyword
   - OR tags contain the keyword
3. Group matches by user
4. Format and send Telegram messages
"""

from __future__ import annotations

from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from src.common.logger import get_logger
from src.common.models import Subscription, _escape_md
from src.config import get_settings

logger = get_logger(__name__)


class KeywordMatcher:
    """Match jobs to user keyword subscriptions."""

    def __init__(self):
        self.settings = get_settings()

    def match_jobs_to_subscription(
        self,
        jobs: list[dict],
        subscription: Subscription,
    ) -> list[dict]:
        """Find jobs matching a subscription keyword.

        Args:
            jobs: List of job dicts from DynamoDB.
            subscription: User subscription with keyword.

        Returns:
            List of matching job dicts.
        """
        keyword = subscription.keyword_normalized
        matches: list[dict] = []

        for job in jobs:
            if self._is_match(job, keyword, subscription):
                matches.append(job)

        return matches

    def _is_match(
        self,
        job: dict,
        keyword: str,
        subscription: Subscription,
    ) -> bool:
        """Check if a job matches a keyword subscription.

        Matching rules:
        1. keyword found in title_normalized (main match)
        2. keyword found in tags list
        3. Optional: location filter
        4. Optional: salary filter
        """
        from src.matcher.search import relevance, matches_filters
        return bool(relevance(job, keyword)) and matches_filters(
            job, subscription.location_filter, subscription.salary_min_filter,
            max_age_days=self.settings.max_job_age_days,
        )

    def get_new_jobs_since(
        self,
        table_name: str,
        since: datetime,
    ) -> list[dict]:
        """Get jobs scraped since a given time.

        Args:
            table_name: DynamoDB jobs table name.
            since: Cutoff time — only return jobs scraped after this.

        Returns:
            List of job dicts.
        """
        dynamodb = boto3.resource("dynamodb", region_name=self.settings.aws_region)
        table = dynamodb.Table(table_name)

        try:
            response = table.scan(
                FilterExpression="scraped_at >= :since",
                ExpressionAttributeValues={
                    ":since": since.isoformat(),
                },
            )

            items = response.get("Items", [])

            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression="scraped_at >= :since",
                    ExpressionAttributeValues={
                        ":since": since.isoformat(),
                    },
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            logger.info(f"Found {len(items)} new jobs since {since.isoformat()}")
            return items

        except ClientError as e:
            logger.error(f"Failed to get new jobs: {e}")
            return []

    def get_all_active_subscriptions(self, table_name: str) -> dict[str, list[Subscription]]:
        """Get all active subscriptions grouped by user_id.

        Args:
            table_name: DynamoDB subscriptions table name.

        Returns:
            Dict mapping user_id to list of Subscription objects.
        """
        dynamodb = boto3.resource("dynamodb", region_name=self.settings.aws_region)
        table = dynamodb.Table(table_name)

        user_subs: dict[str, list[Subscription]] = {}

        kwargs = {
            "FilterExpression": "is_active = :active AND begins_with(sk, :sub_prefix)",
            "ExpressionAttributeValues": {":active": True, ":sub_prefix": "SUB#"},
        }
        while True:
            response = table.scan(**kwargs)
            for item in response.get("Items", []):
                sub = Subscription(
                    user_id=str(item["user_id"]),
                    keyword_raw=item.get("keyword_raw", ""),
                    keyword_normalized=item.get("keyword_normalized", ""),
                    location_filter=item.get("location_filter") or None,
                    salary_min_filter=item.get("salary_min_filter") or None,
                )
                if sub.keyword_normalized.strip():
                    user_subs.setdefault(sub.user_id, []).append(sub)
            if not response.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        logger.info("Loaded subscriptions for %s users", len(user_subs))
        return user_subs


def format_notification(
    keyword: str,
    matched_jobs: list[dict],
    max_jobs: int = 10,
) -> str:
    """Format matched jobs into a Telegram notification message.

    Args:
        keyword: The subscription keyword.
        matched_jobs: List of matching job dicts.
        max_jobs: Maximum jobs to include in one message.

    Returns:
        Formatted Telegram message (MarkdownV2).
    """
    if not matched_jobs:
        return ""

    jobs_to_show = matched_jobs[:max_jobs]
    remaining = len(matched_jobs) - len(jobs_to_show)

    header = (
        f"🔔 *{len(matched_jobs)} việc mới* cho "
        f"*{_escape_md(keyword)}*\\!\n\n"
    )

    body_parts = []
    for i, job in enumerate(jobs_to_show, 1):
        title = _escape_md(job.get("title", "N/A"))
        company = _escape_md(job.get("company", "N/A"))
        location = _escape_md(job.get("location", ""))
        salary = _escape_md(job.get("salary_raw", "Thỏa thuận"))
        url = job.get("source_url", "")

        entry = (
            f"*{i}\\. {title}*\n"
            f"   🏢 {company}\n"
            f"   📍 {location}\n"
            f"   💰 {salary}\n"
        )
        if url:
            entry += f"   🔗 [Xem chi tiết]({url})\n"

        body_parts.append(entry)

    body = "\n".join(body_parts)

    footer = ""
    if remaining > 0:
        footer = f"\n_\\.\\.\\.và {remaining} việc khác\\. Dùng /search {_escape_md(keyword)} để xem tất cả\\._"

    return header + body + footer
