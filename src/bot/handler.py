"""
Telegram Bot Handler for ViecLamBot.

Handles user commands via Telegram Bot API:
- /start          — Welcome message & registration
- /subscribe <kw> — Subscribe to job keyword alerts
- /unsubscribe <kw> — Unsubscribe from keyword
- /list           — List active subscriptions
- /search <kw>    — Search current jobs by keyword
- /help           — Show help message

Designed to work with AWS API Gateway + Lambda webhook.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
import requests
from botocore.exceptions import ClientError

from src.common.logger import get_logger
from src.common.models import Subscription, User, _escape_md
from src.config import get_settings
from src.etl.loader import DynamoDBLoader

logger = get_logger(__name__)


# Simple in-memory set to deduplicate Telegram webhook retries within the
# same Lambda container.  Because Lambda containers are reused across
# invocations, this set persists for the lifetime of the container and
# prevents the same update_id from being processed more than once.
_processed_update_ids: set[int] = set()
_MAX_PROCESSED_IDS = 500  # cap to avoid unbounded memory growth


MENU_SEARCH = "🔎 Tìm việc"
MENU_MY_JOBS = "🎯 Việc phù hợp"
MENU_SUBSCRIBE = "🔔 Tạo thông báo"
MENU_SUBSCRIPTIONS = "📋 Đăng ký của tôi"
MENU_MORE = "➡️ Xem thêm"
MENU_WEB_SEARCH = "🌐 Tìm thêm trên web"
MENU_UNSUBSCRIBE = "🗑 Hủy thông báo"
MENU_HELP = "❓ Hướng dẫn"
MENU_ACTIONS = {
    MENU_SEARCH,
    MENU_MY_JOBS,
    MENU_SUBSCRIBE,
    MENU_SUBSCRIPTIONS,
    MENU_MORE,
    MENU_WEB_SEARCH,
    MENU_UNSUBSCRIBE,
    MENU_HELP,
}


def main_menu_markup() -> dict[str, Any]:
    """Return the persistent Telegram reply keyboard used by the bot."""
    return {
        "keyboard": [
            [{"text": MENU_SEARCH}, {"text": MENU_MY_JOBS}],
            [{"text": MENU_SUBSCRIBE}, {"text": MENU_SUBSCRIPTIONS}],
            [{"text": MENU_MORE}, {"text": MENU_WEB_SEARCH}],
            [{"text": MENU_UNSUBSCRIBE}, {"text": MENU_HELP}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Nhập nghề hoặc kỹ năng cần tìm…",
    }


class TelegramBot:
    """Telegram Bot handler for ViecLamBot."""

    def __init__(self):
        self.settings = get_settings()
        self.token = self.settings.telegram_bot_token
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.db_loader = DynamoDBLoader()
        self._you_search_client = None
        self._request_deadline_at: float | None = None

    # ── Telegram API Methods ────────────────────────────────────

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "MarkdownV2",
        disable_preview: bool = True,
        reply_markup: dict[str, Any] | None = None,
    ) -> Optional[dict]:
        """Send a message via Telegram Bot API.

        Args:
            chat_id: Telegram chat ID.
            text: Message text.
            parse_mode: Formatting mode.
            disable_preview: Disable link preview.

        Returns:
            The sent message dict on success, None on failure.
        """
        try:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": disable_preview,
                "reply_markup": reply_markup or main_menu_markup(),
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode

            response = requests.post(
                f"{self.api_url}/sendMessage",
                json=payload,
                timeout=10,
            )

            if response.status_code == 429:
                retry_after = int(response.json().get("parameters", {}).get("retry_after", 1))
                if retry_after <= 3:
                    time.sleep(max(1, retry_after))
                    response = requests.post(
                        f"{self.api_url}/sendMessage", json=payload, timeout=10
                    )

            if response.status_code != 200:
                logger.error(
                    f"Telegram send failed: {response.status_code} {response.text[:500]}"
                )
                # Fall back to plain text if MarkdownV2 fails
                if (parse_mode == "MarkdownV2" and response.status_code == 400
                        and "parse entities" in response.text.lower()):
                    # Strip MarkdownV2 escape characters for clean plain text
                    import re
                    plain_text = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!])', r'\1', text)
                    return self.send_message(
                        chat_id,
                        plain_text,
                        parse_mode="",
                        disable_preview=disable_preview,
                        reply_markup=reply_markup,
                    )
                return None

            return response.json().get("result")

        except Exception as e:
            # Request exception strings contain the bot token in the URL.
            logger.error("Telegram send exception: %s", type(e).__name__)
            return None

    def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: str = "MarkdownV2",
        disable_preview: bool = True,
    ) -> Optional[dict]:
        """Edit an existing message via Telegram Bot API."""
        try:
            response = requests.post(
                f"{self.api_url}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": disable_preview,
                },
                timeout=10,
            )

            if response.status_code != 200:
                if (
                    response.status_code == 400
                    and "message is not modified" in response.text.lower()
                ):
                    return {"message_id": message_id}
                logger.error(
                    f"Telegram edit failed: {response.status_code} {response.text}"
                )
                if (parse_mode == "MarkdownV2" and response.status_code == 400
                        and "parse entities" in response.text.lower()):
                    return self.edit_message(
                        chat_id,
                        message_id,
                        text,
                        parse_mode="",
                        disable_preview=disable_preview,
                    )
                return None

            return response.json().get("result")

        except Exception as e:
            logger.error("Telegram edit exception: %s", type(e).__name__)
            return None


    # ── Command Handlers ────────────────────────────────────────

    def _user_table(self):
        return boto3.resource("dynamodb", region_name=self.settings.aws_region).Table(
            self.settings.dynamodb_users_table
        )

    def _claim_update(self, update_id: int) -> bool:
        """Persistently claim an update so Telegram retries cannot run it twice."""
        if update_id in _processed_update_ids:
            return False
        try:
            self._user_table().put_item(
                Item={
                    "user_id": "SYSTEM#TELEGRAM_UPDATES",
                    "sk": f"UPDATE#{update_id}",
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "ttl": int(time.time()) + 86400,
                },
                ConditionExpression="attribute_not_exists(user_id)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.info("Skipping duplicate update_id=%s", update_id)
                return False
            # Keep the bot available during a transient DynamoDB problem. The
            # in-memory guard still protects retries handled by this container.
            logger.warning("Persistent update claim failed: %s", type(exc).__name__)
        _processed_update_ids.add(update_id)
        if len(_processed_update_ids) > _MAX_PROCESSED_IDS:
            for uid in list(_processed_update_ids)[: _MAX_PROCESSED_IDS // 2]:
                _processed_update_ids.discard(uid)
        return True

    def _set_pending_action(self, chat_id: str, action: str) -> None:
        self._user_table().put_item(
            Item={
                "user_id": chat_id,
                "sk": "BOT#STATE",
                "action": action,
                "ttl": int(time.time()) + 600,
            }
        )

    def _pop_pending_action(self, chat_id: str) -> str:
        key = {"user_id": chat_id, "sk": "BOT#STATE"}
        table = self._user_table()
        item = table.get_item(Key=key, ConsistentRead=True).get("Item", {})
        if item:
            table.delete_item(Key=key)
        if int(item.get("ttl", 0)) < int(time.time()):
            return ""
        return str(item.get("action", ""))

    def _clear_pending_action(self, chat_id: str) -> None:
        self._user_table().delete_item(Key={"user_id": chat_id, "sk": "BOT#STATE"})

    def _prompt_search(self, chat_id: str) -> None:
        self._set_pending_action(chat_id, "search")
        self.send_message(
            chat_id,
            "🔎 Hãy nhập nghề hoặc kỹ năng. Có thể thêm khu vực sau dấu |.\n"
            "Ví dụ: data analyst | HCM",
            parse_mode="",
        )

    def _prompt_web_search(self, chat_id: str) -> None:
        self._set_pending_action(chat_id, "web_search")
        self.send_message(
            chat_id,
            "🌐 Nhập nghề hoặc kỹ năng để tìm thêm trên Internet. "
            "Có thể thêm khu vực sau dấu |.\nVí dụ: oracle intern | HCM",
            parse_mode="",
        )

    def _prompt_subscribe(self, chat_id: str) -> None:
        self._set_pending_action(chat_id, "subscribe")
        self.send_message(
            chat_id,
            "🔔 Nhập nghề hoặc kỹ năng muốn nhận thông báo. "
            "Có thể thêm khu vực sau dấu |.\nVí dụ: business analyst intern | HCM",
            parse_mode="",
        )

    def _prompt_unsubscribe(self, chat_id: str) -> None:
        self._set_pending_action(chat_id, "unsubscribe")
        self.send_message(
            chat_id,
            "🗑 Nhập đúng từ khóa muốn ngừng theo dõi, hoặc nhập all để hủy tất cả.",
            parse_mode="",
        )

    def handle_webhook(self, body: dict, deadline_at: float | None = None) -> dict:
        """Handle incoming Telegram webhook event.

        Args:
            body: Parsed webhook JSON body.

        Returns:
            Response dict for API Gateway.
        """
        try:
            # ── Deduplication: prevent Telegram retry loops ──────────
            update_id = body.get("update_id")
            if update_id is not None and not self._claim_update(int(update_id)):
                return {"statusCode": 200, "body": "OK"}

            self._request_deadline_at = deadline_at

            message = body.get("message", {})
            if not message:
                return {"statusCode": 200, "body": "OK"}

            chat_id = str(message["chat"]["id"])
            text = message.get("text", "").strip()
            user_info = message.get("from", {})

            if not text:
                return {"statusCode": 200, "body": "OK"}

            # Accept /command@BotName in groups.
            command, _, arguments = text.partition(" ")
            if command.startswith("/"):
                text = command.split("@", 1)[0] + (" " + arguments if arguments else "")
                try:
                    self._clear_pending_action(chat_id)
                except ClientError:
                    logger.warning("Could not clear pending bot action for chat_id=%s", chat_id)
            elif text in MENU_ACTIONS:
                try:
                    self._clear_pending_action(chat_id)
                except ClientError:
                    logger.warning("Could not reset pending bot action for chat_id=%s", chat_id)

            # Route commands
            if text.startswith("/start"):
                self._handle_start(chat_id, user_info)
            elif text.startswith("/menu"):
                self._show_menu(chat_id)
            elif text.startswith("/subscribe"):
                keyword = text.replace("/subscribe", "").strip()
                if keyword:
                    self._handle_subscribe(chat_id, keyword)
                else:
                    self._prompt_subscribe(chat_id)
            elif text.startswith("/unsubscribe"):
                keyword = text.replace("/unsubscribe", "").strip()
                if keyword:
                    self._handle_unsubscribe(chat_id, keyword)
                else:
                    self._prompt_unsubscribe(chat_id)
            elif text.startswith("/list"):
                self._handle_list(chat_id)
            elif text.startswith("/myjobs") or text.startswith("/jobs"):
                self._handle_myjobs(chat_id)
            elif text.startswith("/search"):
                keyword = text.replace("/search", "").strip()
                if keyword:
                    self._handle_search(chat_id, keyword)
                else:
                    self._prompt_search(chat_id)
            elif text.startswith("/web"):
                keyword = text.replace("/web", "", 1).strip()
                if keyword:
                    self._handle_web_search(chat_id, keyword)
                else:
                    self._prompt_web_search(chat_id)
            elif text == "/more":
                self._handle_more(chat_id)
            elif text.startswith("/health"):
                self._handle_health(chat_id)
            elif text.startswith("/help"):
                self._handle_help(chat_id)
            elif text.startswith("/cancel"):
                self.send_message(chat_id, "Đã hủy thao tác.", parse_mode="")
            elif text == MENU_SEARCH:
                self._prompt_search(chat_id)
            elif text == MENU_MY_JOBS:
                self._handle_myjobs(chat_id)
            elif text == MENU_SUBSCRIBE:
                self._prompt_subscribe(chat_id)
            elif text == MENU_SUBSCRIPTIONS:
                self._handle_list(chat_id)
            elif text == MENU_MORE:
                self._handle_more(chat_id)
            elif text == MENU_WEB_SEARCH:
                self._prompt_web_search(chat_id)
            elif text == MENU_UNSUBSCRIBE:
                self._prompt_unsubscribe(chat_id)
            elif text == MENU_HELP:
                self._handle_help(chat_id)
            elif text.startswith("/"):
                self.send_message(
                    chat_id,
                    "Lệnh chưa được hỗ trợ. Hãy chọn một chức năng trên menu.",
                    parse_mode="",
                )
            else:
                pending_action = self._pop_pending_action(chat_id)
                if pending_action == "subscribe":
                    self._handle_subscribe(chat_id, text)
                elif pending_action == "unsubscribe":
                    self._handle_unsubscribe(chat_id, text)
                elif pending_action == "web_search":
                    self._handle_web_search(chat_id, text)
                else:
                    # Search is also the default for ordinary text.
                    self._handle_search(chat_id, text)

            return {"statusCode": 200, "body": "OK"}

        except Exception as e:
            logger.error(f"Webhook handler error: {e}", exc_info=True)
            return {"statusCode": 200, "body": "OK"}  # Always return 200 to Telegram
        finally:
            self._request_deadline_at = None

    def _show_menu(self, chat_id: str) -> None:
        self.send_message(
            chat_id,
            "🏠 Menu chính\n\nChọn một chức năng bên dưới hoặc gõ trực tiếp tên nghề để tìm.",
            parse_mode="",
        )

    def _handle_start(self, chat_id: str, user_info: dict) -> None:
        """Handle /start command — register user."""
        # Save user to DynamoDB
        user = User(
            user_id=chat_id,
            username=user_info.get("username", ""),
            first_name=user_info.get("first_name", ""),
        )

        try:
            dynamodb = boto3.resource("dynamodb", region_name=self.settings.aws_region)
            table = dynamodb.Table(self.settings.dynamodb_users_table)
            table.put_item(Item=user.to_dynamo_item())
        except ClientError as e:
            logger.error(f"Failed to save user: {e}")

        self.send_message(chat_id,
            "🇻🇳 ViecLamBot — tìm việc và nhận tin tuyển dụng\n\n"
            "Gõ tên nghề hoặc kỹ năng để tìm ngay, có dấu hay không dấu đều được.\n"
            "Ví dụ: kế toán tại HCM, python | Hà Nội\n\n"
            "Chọn nút trên menu để tìm việc, tạo thông báo hoặc xem các đăng ký.\n\n"
            f"Tối đa {self.settings.max_subscriptions} từ khóa. "
            f"Tin mới được kiểm tra mỗi {self.settings.alert_interval_hours} giờ.",
            parse_mode="")

    def _handle_subscribe(self, chat_id: str, keyword: str) -> None:
        """Handle /subscribe command."""
        if not keyword:
            self.send_message(
                chat_id,
                "⚠️ Vui lòng nhập từ khóa\\.\n"
                "_Ví dụ: /subscribe data engineer_ hoặc _/subscribe python | hà nội_",
            )
            return

        from src.matcher.search import concepts, normalize_location, parse_search
        keyword_clean, location_raw = parse_search(keyword)
        if not concepts(keyword_clean):
            self.send_message(
                chat_id,
                "Vui lòng nhập nghề hoặc kỹ năng để nhận alert.",
                parse_mode="",
            )
            return
        location_normalized = normalize_location(location_raw) if location_raw else None

        sub = Subscription(
            user_id=chat_id,
            keyword_raw=keyword_clean,
            location_filter=location_normalized
        )

        try:
            dynamodb = boto3.resource("dynamodb", region_name=self.settings.aws_region)
            table = dynamodb.Table(self.settings.dynamodb_users_table)

            # Query all existing subscriptions for the user
            response = table.query(
                KeyConditionExpression="user_id = :uid AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={
                    ":uid": chat_id,
                    ":prefix": "SUB#",
                },
            )
            existing_subs = response.get("Items", [])

            # Check if subscription already exists
            sub_sk = f"SUB#{sub.keyword_normalized}"
            already_exists = any(item.get("sk") == sub_sk for item in existing_subs)

            if already_exists:
                table.update_item(
                    Key={"user_id": chat_id, "sk": sub_sk},
                    UpdateExpression="SET location_filter = :loc, is_active = :active",
                    ExpressionAttributeValues={":loc": location_normalized or "", ":active": True},
                )
                self.send_message(chat_id,
                    f"✅ Đã cập nhật đăng ký: {keyword_clean} · {location_raw or 'Mọi địa điểm'}",
                    parse_mode="")
                return

            # Check configured subscription limit
            if len(existing_subs) >= self.settings.max_subscriptions:
                self.send_message(
                    chat_id,
                    "⚠️ *Giới hạn đăng ký:* Mỗi tài khoản chỉ được đăng ký tối đa "
                    f"*{self.settings.max_subscriptions} từ khóa nhận tin*\\.\n\n"
                    "Vui lòng hủy bớt từ khóa cũ trước bằng lệnh:\n"
                    "`/unsubscribe <từ khóa>`",
                )
                return

            # Save subscription
            table.put_item(Item=sub.to_dynamo_item())

            msg = f"✅ Đã đăng ký thành công\\!\n\n🔑 Từ khóa: *{_escape_md(keyword_clean)}*"
            if location_raw:
                msg += f"\n📍 Khu vực: *{_escape_md(location_raw)}*"
            msg += "\n🔔 Bạn sẽ nhận thông báo mỗi 6 tiếng khi có việc mới phù hợp\\."

            self.send_message(chat_id, msg)

        except ClientError as e:
            logger.error(f"Subscribe failed: {e}")
            self.send_message(chat_id, "❌ Có lỗi xảy ra\\. Vui lòng thử lại\\.")

    def _handle_unsubscribe(self, chat_id: str, keyword: str) -> None:
        """Handle /unsubscribe command."""
        try:
            dynamodb = boto3.resource("dynamodb", region_name=self.settings.aws_region)
            table = dynamodb.Table(self.settings.dynamodb_users_table)

            # Query all active subscriptions for the user
            response = table.query(
                KeyConditionExpression="user_id = :uid AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={
                    ":uid": chat_id,
                    ":prefix": "SUB#",
                },
            )
            subs = response.get("Items", [])

            if not subs:
                self.send_message(
                    chat_id,
                    "📋 Bạn chưa đăng ký từ khóa nào\\.\n"
                    "_Dùng /subscribe \\<từ khóa\\> để bắt đầu\\!_",
                )
                return

            # If no keyword is provided, list subscriptions and show how to unsubscribe
            if not keyword:
                subs_text = "\n".join(
                    "  • `/unsubscribe "
                    f"{item.get('keyword_raw', item.get('keyword_normalized', ''))}`"
                    for item in subs
                )
                self.send_message(
                    chat_id,
                    f"⚠️ Vui lòng nhập từ khóa cần hủy\\.\n\n"
                    f"📋 *Các từ khóa bạn đang đăng ký:*\n{subs_text}",
                )
                return

            keyword_normalized = keyword.lower().strip()

            # Find matching subscriptions
            # Match if keyword_normalized is a substring of the subscription key or vice versa
            if keyword_normalized in {"all", "tất cả", "tat ca"}:
                matched_subs = subs
            else:
                matched_subs = []
                for item in subs:
                    sub_kw = item.get("keyword_normalized", "").lower().strip()
                    if not sub_kw:
                        continue
                    if (
                        keyword_normalized == sub_kw
                        or keyword_normalized in sub_kw
                        or sub_kw in keyword_normalized
                    ):
                        matched_subs.append(item)

            if not matched_subs:
                subs_text = "\n".join(
                    "  • `/unsubscribe "
                    f"{item.get('keyword_raw', item.get('keyword_normalized', ''))}`"
                    for item in subs
                )
                self.send_message(
                    chat_id,
                    f"❌ Không tìm thấy từ khóa nào khớp với *{_escape_md(keyword)}*\\.\n\n"
                    f"📋 *Các từ khóa bạn đang đăng ký:*\n{subs_text}",
                )
                return

            # Delete matched subscriptions
            deleted_kws = []
            for item in matched_subs:
                table.delete_item(
                    Key={"user_id": chat_id, "sk": item["sk"]}
                )
                deleted_kws.append(item.get("keyword_raw", item.get("keyword_normalized", "")))

            deleted_text = "\n".join(f"  • *{_escape_md(kw)}*" for kw in deleted_kws)
            self.send_message(
                chat_id,
                f"🗑️ *Đã hủy đăng ký thành công các từ khóa sau:*\n{deleted_text}",
            )

        except ClientError as e:
            logger.error(f"Unsubscribe failed: {e}")
            self.send_message(chat_id, "❌ Có lỗi xảy ra\\. Vui lòng thử lại\\.")

    def _handle_list(self, chat_id: str) -> None:
        """Handle /list command — show active subscriptions."""
        try:
            dynamodb = boto3.resource("dynamodb", region_name=self.settings.aws_region)
            table = dynamodb.Table(self.settings.dynamodb_users_table)

            response = table.query(
                KeyConditionExpression="user_id = :uid AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={
                    ":uid": chat_id,
                    ":prefix": "SUB#",
                },
            )

            items = response.get("Items", [])

            if not items:
                self.send_message(
                    chat_id,
                    "📋 Bạn chưa đăng ký từ khóa nào\\.\n"
                    "_Dùng /subscribe \\<từ khóa\\> để bắt đầu\\!_",
                )
                return

            subs_text = "\n".join(
                f"  • *{_escape_md(item.get('keyword_raw', item.get('keyword_normalized', '')))}*"
                for item in items
                if item.get("is_active", True)
            )

            self.send_message(
                chat_id,
                f"📋 *Các từ khóa đã đăng ký* \\({len(items)}\\):\n\n{subs_text}\n\n"
                f"_Dùng /unsubscribe \\<từ khóa\\> để hủy\\._",
            )

        except ClientError as e:
            logger.error(f"List subs failed: {e}")
            self.send_message(chat_id, "❌ Có lỗi xảy ra\\. Vui lòng thử lại\\.")

    def _save_search(self, chat_id: str, jobs: list[dict], heading: str):
        from src.bot.messages import clip
        fields = {"title": 160, "company": 80, "location": 80, "salary_raw": 70,
                  "source": 30, "source_url": 900}
        compact, byte_count = [], 0
        for job in jobs[:self.settings.search_result_limit]:
            item = {key: clip(job.get(key), length) for key, length in fields.items()}
            if len(str(job.get("source_url") or "")) > fields["source_url"]:
                item["source_url"] = ""
            byte_count += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            if byte_count > 280000:
                break
            compact.append(item)
        self._user_table().put_item(Item={
            "user_id": chat_id, "sk": "SEARCH#LAST", "jobs": compact,
            "heading": clip(heading, 160), "offset": 0,
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 3600,
        })

    def _handle_more(self, chat_id: str, message_id: int | None = None):
        from src.bot.messages import job_pages
        key = {"user_id": chat_id, "sk": "SEARCH#LAST"}
        table = self._user_table()
        state = table.get_item(Key=key, ConsistentRead=True).get("Item", {})
        jobs = state.get("jobs", [])
        offset = int(state.get("offset", 0))
        if int(state.get("ttl", 0)) < datetime.now(timezone.utc).timestamp() or offset >= len(jobs):
            self.send_message(
                chat_id,
                "Đã hết kết quả hoặc phiên tìm đã hết hạn. Dùng /search để tìm lại.",
                parse_mode="",
            )
            return
        message, included = next(job_pages(jobs[offset:], state["heading"]))
        next_offset = offset + len(included)
        message += f"\nĐang xem {offset + 1}–{next_offset}/{len(jobs)} kết quả."
        if next_offset < len(jobs):
            message += "\n/more — Xem tiếp"
        if message_id:
            success = self.edit_message(chat_id, message_id, message, parse_mode="HTML")
            if not success:
                success = self.send_message(chat_id, message, parse_mode="HTML")
        else:
            success = self.send_message(chat_id, message, parse_mode="HTML")
        if success:
            table.update_item(Key=key, UpdateExpression="SET #offset = :offset",
                              ExpressionAttributeNames={"#offset": "offset"},
                              ExpressionAttributeValues={":offset": next_offset})

    def _handle_myjobs(self, chat_id: str) -> None:
        from src.matcher.search import identity
        try:
            kwargs = {
                "KeyConditionExpression": "user_id = :uid AND begins_with(sk, :prefix)",
                "ExpressionAttributeValues": {":uid": chat_id, ":prefix": "SUB#"},
            }
            items = []
            while True:
                response = self._user_table().query(**kwargs)
                items.extend(response.get("Items", []))
                if not response.get("LastEvaluatedKey"):
                    break
                kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            all_jobs, seen = [], set()
            for item in items:
                if not item.get("is_active", True):
                    continue
                results = self.db_loader.search_jobs(
                    item.get("keyword_raw") or item.get("keyword_normalized", ""),
                    limit=self.settings.search_result_limit,
                    location=item.get("location_filter"),
                    salary_min=item.get("salary_min_filter"),
                )
                for job in results:
                    if identity(job) not in seen:
                        seen.add(identity(job))
                        all_jobs.append(job)
            if not all_jobs:
                self.send_message(
                    chat_id,
                    "Chưa có việc phù hợp. Dùng /list để kiểm tra từ khóa "
                    "hoặc /subscribe để đăng ký.",
                    parse_mode="",
                )
                return
            self._save_search(chat_id, all_jobs, "📋 Việc phù hợp với đăng ký của bạn")
            self._handle_more(chat_id)
        except Exception:
            logger.exception("My jobs failed")
            self.send_message(
                chat_id,
                "Không tải được dữ liệu lúc này. Vui lòng thử lại sau.",
                parse_mode="",
            )

    def _handle_search(self, chat_id: str, keyword: str) -> None:
        from datetime import datetime, timedelta, timezone

        from src.matcher.search import concepts, parse_search
        query, location = parse_search(keyword)
        if not concepts(query):
            self.send_message(
                chat_id,
                "Nhập nghề hoặc kỹ năng cần tìm, ví dụ: /search kế toán | HCM",
                parse_mode="",
            )
            return
        temp = self.send_message(chat_id, "🔍 Đang tìm việc phù hợp…", parse_mode="")
        message_id = temp.get("message_id") if temp else None
        try:
            web_jobs = []
            web_search_failed = False
            if self.settings.you_search_enabled:
                try:
                    web_jobs = self._search_web(
                        query,
                        location,
                        limit=min(20, self.settings.search_result_limit),
                    )
                except Exception:
                    web_search_failed = True
                    logger.exception("You.com API-first search failed")
            database_jobs = []
            if not self.settings.you_search_enabled or web_search_failed:
                database_jobs = self.db_loader.search_jobs(
                    query,
                    limit=self.settings.search_result_limit,
                    location=location,
                    posted_since=datetime.now(timezone.utc)
                    - timedelta(days=self.settings.interactive_search_max_age_days),
                    deadline_at=self._request_deadline_at,
                )
            jobs = self._merge_unique_jobs(web_jobs, database_jobs)
            if not jobs:
                message = f"Chưa có việc phù hợp với ‘{query}’"
                if location:
                    message += f" tại {location}"
                message += ". Thử tên nghề ngắn hơn hoặc /subscribe để nhận tin khi có việc mới."
                if message_id:
                    if self.edit_message(chat_id, message_id, message, parse_mode=""):
                        return
                self.send_message(chat_id, message, parse_mode="")
                return
            heading = f"🔍 {query}" + (f" · {location}" if location else "")
            if web_jobs:
                heading += " · web mới trước"
            self._save_search(chat_id, jobs, heading)
            self._handle_more(chat_id, message_id)
        except Exception:
            logger.exception("Search failed")
            message = "Không tải được dữ liệu lúc này. Vui lòng thử lại sau."
            if message_id and self.edit_message(chat_id, message_id, message, parse_mode=""):
                return
            self.send_message(chat_id, message, parse_mode="")

    def _search_web(
        self,
        query: str,
        location: str | None,
        *,
        limit: int,
    ) -> list[dict]:
        from src.web_search import YouSearchClient, search_live_job_boards

        if self._you_search_client is None:
            self._you_search_client = YouSearchClient(self.settings)
        provider_jobs = self._you_search_client.search(
            query, location=location, limit=limit, deadline_at=self._request_deadline_at
        )
        if len(provider_jobs) >= min(5, limit):
            return provider_jobs
        live_jobs = search_live_job_boards(
            query,
            location=location,
            limit=limit - len(provider_jobs),
            deadline_at=self._request_deadline_at,
        )
        return self._merge_unique_jobs(provider_jobs, live_jobs)[:limit]

    @staticmethod
    def _merge_unique_jobs(primary: list[dict], extra: list[dict]) -> list[dict]:
        from src.matcher.search import identity

        merged, seen = [], set()
        for job in [*primary, *extra]:
            key = identity(job)
            if key in seen:
                continue
            seen.add(key)
            merged.append(job)
        return merged

    def _handle_web_search(self, chat_id: str, keyword: str) -> None:
        from src.matcher.search import concepts, parse_search
        from src.web_search import YouSearchError

        query, location = parse_search(keyword)
        if not concepts(query):
            self.send_message(
                chat_id,
                "Nhập nghề hoặc kỹ năng cần tìm, ví dụ: /web data analyst intern | HCM",
                parse_mode="",
            )
            return
        if not self.settings.you_search_enabled:
            self.send_message(
                chat_id,
                "Tìm kiếm web chưa được bật trên hệ thống.",
                parse_mode="",
            )
            return

        temp = self.send_message(
            chat_id,
            "🌐 Đang tìm và kiểm tra việc làm mới trên Internet…",
            parse_mode="",
        )
        message_id = temp.get("message_id") if temp else None
        try:
            jobs = self._search_web(query, location, limit=20)
            if not jobs:
                message = (
                    f"Chưa tìm thấy tin tuyển dụng web đáng tin cậy cho ‘{query}’. "
                    "Hãy thử tên nghề ngắn hơn hoặc bỏ bớt địa điểm."
                )
                if message_id and self.edit_message(chat_id, message_id, message, parse_mode=""):
                    return
                self.send_message(chat_id, message, parse_mode="")
                return
            heading = f"🌐 Kết quả web · {query}" + (
                f" · {location}" if location else ""
            )
            self._save_search(chat_id, jobs, heading)
            self._handle_more(chat_id, message_id)
        except YouSearchError as exc:
            message = f"Không thể tìm trên web lúc này: {exc}."
            if message_id and self.edit_message(chat_id, message_id, message, parse_mode=""):
                return
            self.send_message(chat_id, message, parse_mode="")
        except Exception:
            logger.exception("Web search failed")
            message = "Tìm kiếm web đang có lỗi. Vui lòng thử lại sau."
            if message_id and self.edit_message(chat_id, message_id, message, parse_mode=""):
                return
            self.send_message(chat_id, message, parse_mode="")

    def _handle_help(self, chat_id: str) -> None:
        self.send_message(chat_id,
            "📖 Cách dùng ViecLamBot\n\n"
            "/search kế toán | HCM — Tìm theo nghề và địa điểm\n"
            "/web data analyst intern | HCM — Tìm thêm việc mới trên Internet\n"
            "Bạn cũng có thể gõ: tìm việc kế toán tại HCM\n"
            "/more — Xem tiếp kết quả, phiên tìm giữ trong 1 giờ\n"
            "/subscribe python | Hà Nội — Nhận tin phù hợp\n"
            "Gửi lại /subscribe cùng từ khóa để đổi địa điểm.\n"
            "/list — Xem từ khóa đang theo dõi\n"
            "/myjobs — Tổng hợp việc từ các đăng ký\n"
            "/unsubscribe python — Hủy một từ khóa\n"
            "/unsubscribe all — Hủy tất cả\n\n"
            f"Tối đa {self.settings.max_subscriptions} từ khóa, "
            f"cập nhật mỗi {self.settings.alert_interval_hours} giờ. "
            "Kết quả ưu tiên độ phù hợp và tin gần đây. Lương chưa công bố vẫn được hiển thị.",
            parse_mode="")

    def _handle_health(self, chat_id: str) -> None:
        """Handle /health command — show scraper health status (admin only)."""
        # Check admin permission
        if chat_id != self.settings.admin_chat_id:
            self.send_message(
                chat_id,
                "⚠️ Lệnh này chỉ dành cho admin\\.",
            )
            return

        try:
            from src.common.scraper_health import get_all_health_records

            records = get_all_health_records(self.settings)

            if not records:
                self.send_message(chat_id, "📊 Chưa có dữ liệu health nào\\.")
                return

            # Build status table
            lines = ["📊 *Trạng thái Scraper*\n"]

            # Status emoji mapping
            status_emoji = {
                "ok": "✅",
                "empty": "⚪",
                "layout_changed": "🟡",
                "blocked": "🔴",
                "error": "❌",
            }

            for record in sorted(records, key=lambda r: r.get("user_id", "")):
                source = record.get("user_id", "").replace("HEALTH#", "")
                status = record.get("status", "unknown")
                emoji = status_emoji.get(status, "❓")
                jobs = record.get("job_count", 0)
                fails = record.get("consecutive_failures", 0)
                duration = record.get("duration_ms", 0)
                updated = record.get("updated_at", "N/A")[:16]  # Trim to minute
                error = record.get("last_error", "")

                source_esc = _escape_md(source)
                status_esc = _escape_md(status)
                error_esc = _escape_md(error[:50]) if error else ""
                updated_esc = _escape_md(updated)

                line = f"{emoji} *{source_esc}*: `{status_esc}` \\| {jobs} jobs \\| {duration}ms"
                if fails > 0:
                    line += f" \\| ⚠️ {fails} fails"
                if error_esc:
                    line += f"\n   _Error: {error_esc}_"
                line += f"\n   _Updated: {updated_esc}_"
                lines.append(line)

            self.send_message(chat_id, "\n\n".join(lines))

        except Exception as e:
            logger.error(f"Health command failed: {e}", exc_info=True)
            self.send_message(chat_id, "❌ Lỗi khi tải trạng thái scraper\\.")
