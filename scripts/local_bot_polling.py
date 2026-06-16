"""
Run the Telegram Bot locally using long polling.
Reads incoming updates from the Telegram Bot API and feeds them to the webhook handler.
This lets you test the bot commands with the real Telegram App and real DynamoDB database!
"""
from __future__ import annotations

import json
import os
import sys
import time
import requests

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lambdas.bot_webhook_handler import handler as bot_handler
from src.config import get_settings

def run_polling():
    settings = get_settings()
    token = settings.telegram_bot_token

    if not token:
        print("Error: VIECLAMBOT_TELEGRAM_BOT_TOKEN is not configured in .env file.")
        return

    print("=" * 60)
    print(f"Telegram Bot Polling started using bot: {token.split(':')[0]}...")
    print("Open Telegram, search for your bot, and send commands like:")
    print("  /start")
    print("  /subscribe data engineer")
    print("  /list")
    print("  /search python")
    print("Press Ctrl+C to stop.")
    print("=" * 60)

    # First, delete webhook so getUpdates works (Telegram doesn't allow both at the same time)
    try:
        requests.post(f"https://api.telegram.org/bot{token}/deleteWebhook", timeout=10)
        print("Disabled webhook to allow local polling.")
    except Exception as e:
        print(f"Warning: Failed to delete webhook: {e}")

    offset = 0
    while True:
        try:
            # Long poll for updates
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"offset": offset, "timeout": 20}
            response = requests.get(url, params=params, timeout=25)
            
            if response.status_code != 200:
                print(f"Telegram API Error ({response.status_code}): {response.text}")
                time.sleep(2)
                continue
            
            data = response.json()
            updates = data.get("result", [])
            
            for update in updates:
                update_id = update["update_id"]
                offset = update_id + 1
                
                print(f"\n[Received Update] ID: {update_id}")
                message = update.get("message", {})
                if message:
                    sender = message.get("from", {}).get("username", "unknown")
                    text = message.get("text", "")
                    print(f"  From: @{sender} | Message: {text}")
                
                # Mock the API Gateway event format and trigger the handler
                event = {"body": json.dumps(update)}
                try:
                    result = bot_handler(event, None)
                    print(f"  Handler execution response status code: {result.get('statusCode')}")
                except Exception as ex:
                    print(f"  Handler failed: {ex}")
                    
        except KeyboardInterrupt:
            print("\nStopping polling...")
            break
        except Exception as e:
            print(f"\nPolling error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    run_polling()
