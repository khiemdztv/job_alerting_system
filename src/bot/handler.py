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


class TelegramBot:
    """Telegram Bot handler for ViecLamBot."""

    def __init__(self):
        self.settings = get_settings()
        self.token = self.settings.telegram_bot_token
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.db_loader = DynamoDBLoader()

    # ── Telegram API Methods ────────────────────────────────────

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "MarkdownV2",
        disable_preview: bool = True,
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
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode

            response = requests.post(
                f"{self.api_url}/sendMessage",
                json=payload,
                timeout=10,
            )

            if response.status_code != 200:
                logger.error(
                    f"Telegram send failed: {response.status_code} {response.text[:500]}"
                )
                # Fall back to plain text if MarkdownV2 fails
                if parse_mode == "MarkdownV2":
                    # Strip MarkdownV2 escape characters for clean plain text
                    import re
                    plain_text = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!])', r'\1', text)
                    return self.send_message(chat_id, plain_text, parse_mode="", disable_preview=disable_preview)
                return None

            return response.json().get("result")

        except Exception as e:
            logger.error(f"Telegram send exception: {e}")
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
                logger.error(
                    f"Telegram edit failed: {response.status_code} {response.text}"
                )
                if parse_mode == "MarkdownV2":
                    return self.edit_message(chat_id, message_id, text, parse_mode="", disable_preview=disable_preview)
                return None

            return response.json().get("result")

        except Exception as e:
            logger.error(f"Telegram edit exception: {e}")
            return None


    # ── Command Handlers ────────────────────────────────────────

    def handle_webhook(self, body: dict) -> dict:
        """Handle incoming Telegram webhook event.

        Args:
            body: Parsed webhook JSON body.

        Returns:
            Response dict for API Gateway.
        """
        try:
            message = body.get("message", {})
            if not message:
                return {"statusCode": 200, "body": "OK"}

            chat_id = str(message["chat"]["id"])
            text = message.get("text", "").strip()
            user_info = message.get("from", {})

            if not text:
                return {"statusCode": 200, "body": "OK"}

            # Route commands
            if text.startswith("/start"):
                self._handle_start(chat_id, user_info)
            elif text.startswith("/subscribe"):
                keyword = text.replace("/subscribe", "").strip()
                self._handle_subscribe(chat_id, keyword)
            elif text.startswith("/unsubscribe"):
                keyword = text.replace("/unsubscribe", "").strip()
                self._handle_unsubscribe(chat_id, keyword)
            elif text.startswith("/list"):
                self._handle_list(chat_id)
            elif text.startswith("/myjobs") or text.startswith("/jobs"):
                self._handle_myjobs(chat_id)
            elif text.startswith("/search"):
                keyword = text.replace("/search", "").strip()
                self._handle_search(chat_id, keyword)
            elif text.startswith("/help"):
                self._handle_help(chat_id)
            else:
                # Treat non-command text as search
                self._handle_search(chat_id, text)

            return {"statusCode": 200, "body": "OK"}

        except Exception as e:
            logger.error(f"Webhook handler error: {e}", exc_info=True)
            return {"statusCode": 200, "body": "OK"}  # Always return 200 to Telegram

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

        welcome = (
            "🇻🇳 *Chào mừng đến với ViecLamBot\\!*\n\n"
            "Bot tự động tìm kiếm việc làm từ nhiều nguồn tuyển dụng Việt Nam "
            "và gửi thông báo mỗi 6 tiếng\\.\n\n"
            "🌐 *Nguồn dữ liệu:* CareerLink, ViecLam24h, ITviec, CareerViet, TimViec365, Jooble\n\n"
            "⚠️ *Lưu ý:* Mỗi tài khoản được đăng ký tối đa *3 từ khóa nhận tin*\\.\n\n"
            "📋 *Các lệnh:*\n"
            "• `/subscribe <từ khóa> [| khu vực]` \\- Đăng ký nhận thông báo \\(Tối đa 3 từ khóa\\)\n"
            "  _Ví dụ: /subscribe kế toán_\n"
            "  _Ví dụ lọc khu vực: /subscribe marketing | hà nội_\n\n"
            "• `/unsubscribe <từ khóa>` \\- Hủy đăng ký nhận tin\n\n"
            "• `/list` \\- Xem danh sách từ khóa đã đăng ký\n\n"
            "• `/myjobs` hoặc `/jobs` \\- Xem việc mới nhất từ các từ khóa đã đăng ký\n\n"
            "• `/search <từ khóa> [| khu vực]` \\- Tìm việc ngay\n"
            "  _Ví dụ: /search nhân sự | hồ chí minh_\n\n"
            "• `/help` \\- Hướng dẫn chi tiết\n\n"
            "💡 _Bạn cũng có thể gõ trực tiếp từ khóa để tìm nhanh\\!_"
        )
        self.send_message(chat_id, welcome)

    def _handle_subscribe(self, chat_id: str, keyword: str) -> None:
        """Handle /subscribe command."""
        if not keyword:
            self.send_message(
                chat_id,
                "⚠️ Vui lòng nhập từ khóa\\.\n_Ví dụ: /subscribe data engineer_ hoặc _/subscribe python | hà nội_",
            )
            return

        # Parse keyword and location filter if present
        keyword_clean = keyword.strip()
        location_raw = None
        location_normalized = None

        if "|" in keyword:
            parts = keyword.split("|", 1)
            keyword_clean = parts[0].strip()
            location_raw = parts[1].strip()
        elif "," in keyword:
            parts = keyword.split(",", 1)
            keyword_clean = parts[0].strip()
            location_raw = parts[1].strip()

        if location_raw:
            from src.etl.transformer import Transformer
            location_normalized = Transformer._normalize_location(location_raw)

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
                self.send_message(
                    chat_id,
                    f"ℹ️ Bạn đã đăng ký từ khóa *{_escape_md(keyword_clean)}* rồi\\!",
                )
                return

            # Check limit of 3 subscriptions
            if len(existing_subs) >= 3:
                self.send_message(
                    chat_id,
                    "⚠️ *Giới hạn đăng ký:* Mỗi tài khoản chỉ được đăng ký tối đa *3 từ khóa nhận tin*\\.\n\n"
                    "Vui lòng hủy bớt từ khóa cũ trước bằng lệnh:\n"
                    "`/unsubscribe <từ khóa>`",
                )
                return

            # Save subscription
            table.put_item(Item=sub.to_dynamo_item())

            msg = f"✅ Đã đăng ký thành công\\!\n\n🔑 Từ khóa: *{_escape_md(keyword_clean)}*"
            if location_raw:
                msg += f"\n📍 Khu vực: *{_escape_md(location_raw)}*"
            msg += f"\n🔔 Bạn sẽ nhận thông báo mỗi 6 tiếng khi có việc mới phù hợp\\."

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
                    f"  • `/unsubscribe {item.get('keyword_raw', item.get('keyword_normalized', ''))}`"
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
            matched_subs = []
            for item in subs:
                sub_kw = item.get("keyword_normalized", "").lower().strip()
                if not sub_kw:
                    continue
                if keyword_normalized == sub_kw or keyword_normalized in sub_kw or sub_kw in keyword_normalized:
                    matched_subs.append(item)

            if not matched_subs:
                subs_text = "\n".join(
                    f"  • `/unsubscribe {item.get('keyword_raw', item.get('keyword_normalized', ''))}`"
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

    def _filter_and_interleave_jobs(self, jobs: list[dict], limit: int = 20) -> list[dict]:
        """Filter jobs to only those posted within 1 week and interleave them by source to prevent dominance."""
        from datetime import datetime, timezone, timedelta
        
        # 1. Filter jobs within 1 week (7 days)
        one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        recent_jobs = []
        
        for job in jobs:
            posted_str = job.get("posted_at") or job.get("scraped_at")
            if not posted_str:
                recent_jobs.append(job)
                continue
                
            try:
                # Handle ISO format datetimes (e.g. 2026-06-16T13:45:00+00:00 or with Z suffix)
                posted_dt = datetime.fromisoformat(posted_str.replace("z", "+00:00").replace("Z", "+00:00"))
                if posted_dt >= one_week_ago:
                    recent_jobs.append(job)
            except Exception:
                recent_jobs.append(job)
                
        # Fallback: if no recent jobs found, use all matches
        if not recent_jobs:
            recent_jobs = jobs
            
        # 2. Interleave jobs by source
        by_source = {}
        for job in recent_jobs:
            src = job.get("source", "unknown").lower()
            if src not in by_source:
                by_source[src] = []
            by_source[src].append(job)
            
        # Sort jobs within each source group chronologically (newest first)
        for src in by_source:
            def get_sort_key(j):
                return j.get("posted_at") or j.get("scraped_at") or ""
            by_source[src].sort(key=get_sort_key, reverse=True)
            
        # Interleave in round-robin fashion
        interleaved = []
        sources = list(by_source.keys())
        sources.sort()  # Deterministic order
        
        if not sources:
            return []
            
        max_len = max(len(by_source[src]) for src in sources)
        for i in range(max_len):
            for src in sources:
                if i < len(by_source[src]):
                    interleaved.append(by_source[src][i])
                    if len(interleaved) >= limit:
                        return interleaved
                        
        return interleaved

    def _handle_myjobs(self, chat_id: str) -> None:
        """Handle /myjobs command — search jobs for all active subscriptions."""
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
                    "_Dùng /subscribe \\<từ khóa\\> để đăng ký nhận tin\\!_",
                )
                return

            # Send a temporary loading message
            temp_msg = self.send_message(
                chat_id,
                "🔍 *Đang tổng hợp việc làm từ các từ khóa đã đăng ký\\.\\.\\.*"
            )

            all_jobs = []
            seen_job_ids = set()

            for item in items:
                keyword = item.get("keyword_raw", item.get("keyword_normalized", ""))
                loc_filter = item.get("location_filter")
                
                if not keyword:
                    continue

                # Search jobs for this subscription
                results = self.db_loader.search_jobs(keyword, limit=30)
                
                # Apply location filter if set
                if loc_filter:
                    results = [
                        r for r in results
                        if loc_filter.lower() in r.get("location_normalized", "").lower()
                        or loc_filter.lower() in r.get("location", "").lower()
                    ]

                for job in results:
                    job_id = job.get("job_id")
                    if job_id not in seen_job_ids:
                        seen_job_ids.add(job_id)
                        # Keep track of which keyword matched it for display
                        job["matched_keyword"] = keyword
                        all_jobs.append(job)

            if not all_jobs:
                msg_not_found = (
                    "🔍 Hiện tại chưa tìm thấy việc mới nào phù hợp với các từ khóa bạn đăng ký\\.\n"
                    "_Hệ thống tự động cập nhật tin tuyển dụng mỗi 6 tiếng\\._"
                )
                if temp_msg:
                    self.edit_message(chat_id, temp_msg["message_id"], msg_not_found)
                else:
                    self.send_message(chat_id, msg_not_found)
                return

            # Filter to 1 week and interleave across sources
            display_jobs = self._filter_and_interleave_jobs(all_jobs, limit=20)

            header = f"📋 *Top {len(display_jobs)} việc làm mới nhất* từ các từ khóa đã đăng ký:\n\n"
            
            entries = []
            for i, job in enumerate(display_jobs, 1):
                title = _escape_md(job.get("title", "N/A"))
                company = _escape_md(job.get("company", "N/A"))
                location = _escape_md(job.get("location", ""))
                salary = _escape_md(job.get("salary_raw", "Thỏa thuận"))
                url = job.get("source_url", "")
                kw_matched = _escape_md(job.get("matched_keyword", ""))
                
                raw_source = job.get("source", "")
                if raw_source.lower() == "itviec":
                    source_display = "ITviec"
                elif raw_source.lower() == "careerlink":
                    source_display = "CareerLink"
                elif raw_source.lower() == "vieclam24h":
                    source_display = "ViecLam24h"
                elif raw_source.lower() == "jooble":
                    source_display = "Jooble"
                elif raw_source.lower() == "careerviet":
                    source_display = "CareerViet"
                elif raw_source.lower() == "timviec365":
                    source_display = "TimViec365"
                else:
                    source_display = raw_source.capitalize()
                source = _escape_md(source_display)

                entry = f"*{i}\\. [{source}] {title}*\n   🏢 {company}\n   📍 {location} \\| 💰 {salary}\n   🔑 Từ khóa: `{kw_matched}`\n"
                if url:
                    entry += f"   🔗 [Xem chi tiết trên {source}]({url})\n"
                entries.append(entry)

            message = header + "\n".join(entries)
            
            if temp_msg:
                self.edit_message(chat_id, temp_msg["message_id"], message)
            else:
                self.send_message(chat_id, message)

        except Exception as e:
            logger.error(f"My jobs command failed: {e}", exc_info=True)
            err_msg = "❌ Có lỗi xảy ra khi tải việc làm đã đăng ký\\. Vui lòng thử lại\\."
            if temp_msg:
                self.edit_message(chat_id, temp_msg["message_id"], err_msg)
            else:
                self.send_message(chat_id, err_msg)

    def _handle_search(self, chat_id: str, keyword: str) -> None:
        """Handle /search command — search jobs immediately."""
        if not keyword:
            self.send_message(
                chat_id,
                "🔍 Nhập từ khóa để tìm kiếm\\.\n_Ví dụ: /search python | hà nội_",
            )
            return

        # Send a temporary searching message
        temp_msg = self.send_message(
            chat_id,
            "🔍 *Đang tìm kiếm việc làm trực tiếp từ các nguồn, vui lòng đợi trong giây lát\\.\\.\\.*"
        )

        # Parse keyword and location filter if present
        keyword_clean = keyword.strip()
        location_raw = None

        if "|" in keyword:
            parts = keyword.split("|", 1)
            keyword_clean = parts[0].strip()
            location_raw = parts[1].strip()
        elif "," in keyword:
            parts = keyword.split(",", 1)
            keyword_clean = parts[0].strip()
            location_raw = parts[1].strip()

        # Trigger live scraping in parallel to cover any industry / keyword immediately
        try:
            from concurrent.futures import ThreadPoolExecutor
            from src.scrapers.careerlink_scraper import CareerLinkScraper
            from src.scrapers.vieclam24h_scraper import ViecLam24hScraper
            from src.scrapers.itviec_scraper import ITviecScraper
            from src.scrapers.careerviet_scraper import CareerVietScraper
            from src.scrapers.timviec365_scraper import TimViec365Scraper
            from src.scrapers.ybox_scraper import YBoxScraper
            from src.scrapers.jooble_scraper import JoobleScraper
            from src.etl.transformer import Transformer

            scrapers = [
                CareerLinkScraper(),
                ViecLam24hScraper(),
                ITviecScraper(),
                CareerVietScraper(),
                TimViec365Scraper(),
                YBoxScraper(),
            ]
            if self.settings.jooble_api_key:
                scrapers.append(JoobleScraper())

            raw_jobs = []
            
            # Scrape 1 page from each source in parallel for speed
            def scrape_one(scraper):
                return scraper.scrape_safe(keyword_clean, max_pages=1)

            with ThreadPoolExecutor(max_workers=len(scrapers)) as executor:
                results_list = list(executor.map(scrape_one, scrapers))
                for res in results_list:
                    raw_jobs.extend(res)

            # Transform and save to DB immediately
            if raw_jobs:
                transformer = Transformer()
                transformed_jobs = transformer.transform_batch(raw_jobs)
                if transformed_jobs:
                    self.db_loader.load_batch(transformed_jobs)

        except Exception as e:
            logger.error(f"Live scrape / ETL error during search: {e}", exc_info=True)

        try:
            results = self.db_loader.search_jobs(keyword_clean, limit=50)

            # Apply location filter if provided
            if location_raw:
                from src.etl.transformer import Transformer
                loc_norm = Transformer._normalize_location(location_raw).lower()
                results = [
                    r for r in results
                    if loc_norm in r.get("location_normalized", "").lower()
                    or loc_norm in r.get("location", "").lower()
                ]

            if not results:
                msg_not_found = f"🔍 Không tìm thấy việc nào cho *{_escape_md(keyword_clean)}*"
                if location_raw:
                    msg_not_found += f" tại *{_escape_md(location_raw)}*"
                msg_not_found += f"\\.\n_Hãy thử từ khóa khác hoặc /subscribe để nhận thông báo khi có việc mới\\._"
                
                if temp_msg:
                    self.edit_message(chat_id, temp_msg["message_id"], msg_not_found)
                else:
                    self.send_message(chat_id, msg_not_found)
                return

            # Filter to 1 week and interleave across sources
            display_results = self._filter_and_interleave_jobs(results, limit=20)
            
            header = f"🔍 *{len(display_results)} kết quả* cho *{_escape_md(keyword_clean)}*"
            if location_raw:
                header += f" tại *{_escape_md(location_raw)}*"
            header += ":\n\n"

            entries = []
            for i, job in enumerate(display_results, 1):
                title = _escape_md(job.get("title", "N/A"))
                company = _escape_md(job.get("company", "N/A"))
                location = _escape_md(job.get("location", ""))
                salary = _escape_md(job.get("salary_raw", "Thỏa thuận"))
                url = job.get("source_url", "")
                
                raw_source = job.get("source", "")
                if raw_source.lower() == "itviec":
                    source_display = "ITviec"
                elif raw_source.lower() == "careerlink":
                    source_display = "CareerLink"
                elif raw_source.lower() == "vieclam24h":
                    source_display = "ViecLam24h"
                elif raw_source.lower() == "jooble":
                    source_display = "Jooble"
                elif raw_source.lower() == "careerviet":
                    source_display = "CareerViet"
                elif raw_source.lower() == "timviec365":
                    source_display = "TimViec365"
                else:
                    source_display = raw_source.capitalize()
                source = _escape_md(source_display)

                entry = f"*{i}\\. [{source}] {title}*\n   🏢 {company}\n   📍 {location}\n   💰 {salary}\n"
                if url:
                    entry += f"   🔗 [Xem chi tiết trên {source}]({url})\n"
                entries.append(entry)

            message = header + "\n".join(entries)
            
            if temp_msg:
                self.edit_message(chat_id, temp_msg["message_id"], message)
            else:
                self.send_message(chat_id, message)

        except Exception as e:
            logger.error(f"Search failed: {e}", exc_info=True)
            err_msg = "❌ Có lỗi xảy ra khi tìm kiếm\\. Vui lòng thử lại\\."
            if temp_msg:
                self.edit_message(chat_id, temp_msg["message_id"], err_msg)
            else:
                self.send_message(chat_id, err_msg)

    def _handle_help(self, chat_id: str) -> None:
        """Handle /help command."""
        help_text = (
            "📖 *Hướng dẫn sử dụng ViecLamBot*\n\n"
            "Bot tự động thu thập việc làm *đa ngành nghề* từ:\n"
            "• CareerLink\\.vn\n"
            "• ViecLam24h\\.vn\n"
            "• ITviec\\.com\n"
            "• CareerViet\\.vn\n"
            "• TimViec365\\.vn\n"
            "• Jooble \\(aggregator\\)\n\n"
            "⚠️ *Giới hạn nhận tin:* Mỗi tài khoản được đăng ký tối đa *3 từ khóa nhận tin* tự động\\.\n\n"
            "📋 *Danh sách lệnh:*\n"
            "• `/subscribe <từ khóa> [| khu vực]` \\- Đăng ký nhận tin tự động mỗi 6 tiếng \\(tối đa 3\\)\\.\n"
            "  _Ví dụ: /subscribe kế toán | hồ chí minh_\n\n"
            "• `/unsubscribe <từ khóa>` \\- Hủy nhận thông báo cho từ khóa đó\\.\n"
            "  _Ví dụ: /unsubscribe kế toán_\n\n"
            "• `/list` \\- Xem danh sách từ khóa đang đăng ký\\.\n\n"
            "• `/myjobs` hoặc `/jobs` \\- Xem nhanh các việc làm mới nhất cho các từ khóa đã đăng ký\\.\n\n"
            "• `/search <từ khóa> [| khu vực]` \\- Tìm nhanh việc làm trực tiếp từ database\\.\n"
            "  _Ví dụ: /search nhân sự | đà nẵng_\n\n"
            "• *Gõ tin nhắn trực tiếp* \\- Bot sẽ tự động tìm kiếm nhanh theo từ khóa\\.\n\n"
            "💡 _Bạn có thể subscribe bất kỳ từ khóa nào \\(kế toán, marketing, y tế, v\\.v\\.\\)\\. "
            "Hệ thống sẽ tự động tìm việc từ tất cả các nguồn\\!_\n\n"
            "📊 Dữ liệu được cập nhật mỗi 6 tiếng\\."
        )
        self.send_message(chat_id, help_text)
