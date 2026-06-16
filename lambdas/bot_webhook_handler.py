"""
Lambda Handler: Telegram Bot Webhook

Triggered by API Gateway when Telegram sends a webhook event.
Routes to the TelegramBot handler.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.bot.handler import TelegramBot
from src.common.logger import get_logger

logger = get_logger(__name__)


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

        bot = TelegramBot()
        result = bot.handle_webhook(body)

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
