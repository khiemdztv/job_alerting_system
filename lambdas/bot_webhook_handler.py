"""
Lambda Handler: Telegram Bot Webhook

Triggered by API Gateway when Telegram sends a webhook event.
Routes to the TelegramBot handler.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.bot.handler import TelegramBot
from src.common.logger import get_logger

logger = get_logger(__name__)
bot = TelegramBot()


def handler(event, context):
    """Lambda handler for Telegram bot webhook.

    Args:
        event: API Gateway event with Telegram update in body.
        context: Lambda context.

    Returns:
        API Gateway response.
    """
    try:
        # Parse body
        body = event.get("body", "{}")
        if isinstance(body, str):
            body = json.loads(body)

        logger.info("Received Telegram webhook event")

        deadline_at = None
        if context is not None:
            remaining = context.get_remaining_time_in_millis() / 1000
            deadline_at = time.monotonic() + max(1, remaining - 12)
        bot.handle_webhook(body, deadline_at=deadline_at)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": True}),
        }

    except Exception as e:
        logger.error(f"Bot webhook handler error: {e}", exc_info=True)
        return {
            "statusCode": 200,  # Always 200 for Telegram
            "body": json.dumps({"ok": True}),
        }
