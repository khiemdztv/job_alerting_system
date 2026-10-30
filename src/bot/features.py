"""Bot v2 features: job buttons, /saved, /history, /settings, /filter, /exclude,
/company, /follow and onboarding.  Mixed into :class:`src.bot.handler.TelegramBot`.

Callback handlers stay cheap — one DynamoDB write plus ``answerCallbackQuery``
(and an ``editMessageText`` for wizards) — so they never approach the 30 s limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.bot import keyboards as kb
from src.bot.filters import (
    CITY_CHOICES,
    LEVEL_CHOICES,
    SALARY_CHOICES,
    UserContext,
    company_matches,
    filters_summary,
    normalize_term,
)
from src.bot.schedule import quiet_window_label
from src.bot.store import MAX_EXCLUDES, MAX_FOLLOWS, PREF_DEFAULTS, UserStore
from src.common.logger import get_logger

logger = get_logger(__name__)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

JOB_ANSWERS = {
    "s": "💾 Đã lưu. Xem lại bằng /saved",
    "h": "🙈 Đã ẩn, tin này sẽ không xuất hiện lại",
    "u": "👍 Cảm ơn! Bot sẽ gợi ý thêm việc tương tự",
    "d": "👎 Đã ẩn tin và giảm ưu tiên công ty này",
    "r": "🗑 Đã bỏ lưu",
}


class BotFeaturesMixin:
    """Requires ``send_message``, ``edit_message``, ``answer_callback``,
    ``_user_table``, ``_save_search``, ``_handle_more`` from TelegramBot."""

    settings: Any
    db_loader: Any

    # ── Shared helpers ───────────────────────────────────────────

    @property
    def store(self) -> UserStore:
        return UserStore(table=self._user_table(), settings=self.settings)

    def _load_context(self, chat_id: str) -> UserContext | None:
        """User filters for result lists; a storage hiccup must not break search."""
        try:
            return self.store.load_context(chat_id)
        except Exception:
            logger.warning("Could not load user context for chat_id=%s", chat_id)
            return None

    def _edit_or_send(self, chat_id: str, message_id, text: str,
                      reply_markup: dict | None = None, parse_mode: str = "") -> None:
        if message_id and self.edit_message(chat_id, message_id, text, parse_mode=parse_mode,
                                            reply_markup=reply_markup):
            return
        self.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)

    # ── Callback queries ─────────────────────────────────────────

    def _handle_callback(self, callback: dict) -> None:
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or (callback.get("from") or {}).get("id") or "")
        data = str(callback.get("data") or "")
        answer = ""
        try:
            if chat_id:
                answer = self._dispatch_callback(chat_id, message, data) or ""
        except Exception:
            logger.exception("Callback %r failed", data[:20])
            answer = "Có lỗi xảy ra, vui lòng thử lại."
        finally:
            # Always answer, or the button spins until Telegram times out.
            self.answer_callback(str(callback.get("id") or ""), answer)

    def _dispatch_callback(self, chat_id: str, message: dict, data: str) -> str:
        message_id = message.get("message_id")
        head, _, rest = data.partition(":")
        if head == "j":
            return self._job_button(chat_id, message, rest)
        if head == "m":
            self._handle_more(chat_id)
            return ""
        if head == "fo":
            return self._follow_from_button(chat_id, message, rest)
        if head == "st":
            return self._settings_button(chat_id, message_id, rest)
        if head == "f":
            return self._filter_button(chat_id, message_id, rest)
        if head in {"o1", "o2", "o3"}:
            return self._onboarding_button(chat_id, message_id, int(head[1]), rest)
        if head == "go":
            return self._shortcut(chat_id, rest)
        return "Nút này đã hết hạn."

    def _job_button(self, chat_id: str, message: dict, rest: str) -> str:
        action, _, rest = rest.partition(":")
        number_text, _, key = rest.partition(":")
        if action not in JOB_ANSWERS or not key:
            return "Nút này đã hết hạn."
        store = self.store
        if action == "h":
            store.hide_job(chat_id, key)
        elif action == "r":
            store.unsave_job(chat_id, key)
        else:
            number = int(number_text) if number_text.isdigit() else 0
            job = kb.parse_card(message, number)
            if not job and action == "s":
                # Rare: Telegram omitted the message. Two reads, still no scraping.
                job = store.find_job_payload(chat_id, key)
            job = job or {}
            if action == "s":
                store.save_job(chat_id, key, job)
            else:
                store.feedback(chat_id, key, "up" if action == "u" else "down", job)
        return JOB_ANSWERS[action]

    def _shortcut(self, chat_id: str, command: str) -> str:
        handlers = {
            "filter": lambda: self._handle_filter(chat_id),
            "exclude": lambda: self._handle_exclude(chat_id, "list"),
            "list": lambda: self._handle_list(chat_id),
            "history": lambda: self._handle_history(chat_id),
            "web": lambda: self._prompt_web_search(chat_id),
            "follow": lambda: self._handle_follow(chat_id, ""),
            "saved": lambda: self._handle_saved(chat_id),
            "settings": lambda: self._handle_settings(chat_id),
            "subscribe": lambda: self._prompt_subscribe(chat_id),
        }
        handler = handlers.get(command)
        if handler is None:
            return "Nút này đã hết hạn."
        handler()
        return ""

    # ── /saved and /history ──────────────────────────────────────

    def _handle_saved(self, chat_id: str) -> None:
        items = self.store.list_saved(chat_id)
        if not items:
            self.send_message(
                chat_id,
                "📌 Bạn chưa lưu tin nào. Bấm 💾 dưới một tin để lưu lại xem sau.",
                parse_mode="",
            )
            return
        jobs = [{**item, "identity": item["sk"][6:]} for item in items]
        self._save_search(chat_id, jobs, f"📌 Tin đã lưu ({len(jobs)})", kind="saved")
        self._handle_more(chat_id)

    def _handle_history(self, chat_id: str) -> None:
        rows = self.store.history(chat_id, days=7)
        if not rows:
            self.send_message(
                chat_id,
                "🕘 7 ngày qua bot chưa gửi tin nào cho bạn. "
                "Dùng /subscribe để nhận thông báo việc mới.",
                parse_mode="",
            )
            return
        jobs = [{**row, "identity": row["sk"][5:]} for row in rows]
        self._save_search(chat_id, jobs, f"🕘 Tin đã gửi 7 ngày qua ({len(jobs)})")
        self._handle_more(chat_id)

    # ── /settings ────────────────────────────────────────────────

    def _settings_text(self, prefs: dict) -> str:
        frequency = kb.FREQUENCY_CHOICES.get(prefs.get("frequency") or "instant", "Ngay khi có")
        max_jobs = int(prefs.get("max_jobs") or 0) or self.settings.alert_max_jobs_per_user
        quiet = "Bật" if prefs.get("quiet_hours") else "Tắt"
        return (
            "⚙️ Cài đặt thông báo\n\n"
            f"🔔 Tần suất: {frequency}\n"
            f"🌙 Giờ im lặng ({quiet_window_label()}): {quiet}\n"
            f"📦 Tối đa mỗi lần: {max_jobs} tin\n"
            "🌐 Ngôn ngữ: Tiếng Việt\n\n"
            "1 lần/ngày gửi lúc khoảng 8 giờ sáng, hằng tuần vào sáng thứ Hai. "
            "Tin đến trong giờ im lặng được giữ lại và gửi sau."
        )

    def _settings_keyboard(self, prefs: dict) -> dict:
        return kb.settings_keyboard(prefs, self.settings.alert_max_jobs_per_user)

    def _handle_settings(self, chat_id: str) -> None:
        prefs = self.store.get_prefs(chat_id)
        self.send_message(chat_id, self._settings_text(prefs), parse_mode="",
                          reply_markup=self._settings_keyboard(prefs))

    def _settings_button(self, chat_id: str, message_id, rest: str) -> str:
        field, _, value = rest.partition(":")
        store = self.store
        if field == "lg":
            return "Hiện bot chỉ hỗ trợ tiếng Việt."
        if field == "fq" and value in kb.FREQUENCY_CHOICES:
            prefs = store.set_pref(chat_id, frequency=value)
            answer = f"Tần suất: {kb.FREQUENCY_CHOICES[value]}"
        elif field == "qh":
            enabled = not bool(store.get_prefs(chat_id).get("quiet_hours"))
            prefs = store.set_pref(chat_id, quiet_hours=enabled)
            answer = "Đã bật giờ im lặng" if enabled else "Đã tắt giờ im lặng"
        elif field == "mx" and value.isdigit() and int(value) in kb.MAX_JOBS_CHOICES:
            prefs = store.set_pref(chat_id, max_jobs=int(value))
            answer = f"Tối đa {value} tin mỗi lần"
        else:
            return "Nút này đã hết hạn."
        self._edit_or_send(chat_id, message_id, self._settings_text(prefs),
                           reply_markup=self._settings_keyboard(prefs))
        return answer

    # ── /filter ──────────────────────────────────────────────────

    @staticmethod
    def _filter_text(prefs: dict) -> str:
        return (
            "🎛 Bộ lọc mặc định\n\n"
            f"{filters_summary(prefs)}\n\n"
            "Áp dụng cho /search, /myjobs và thông báo tạo mới. "
            "Gõ “| địa điểm” khi tìm để tạm dùng địa điểm khác."
        )

    def _handle_filter(self, chat_id: str) -> None:
        prefs = self.store.get_prefs(chat_id)
        self.send_message(chat_id, self._filter_text(prefs), parse_mode="",
                          reply_markup=kb.filter_home_keyboard())

    def _filter_button(self, chat_id: str, message_id, rest: str) -> str:
        category, _, value = rest.partition(":")
        store = self.store
        if category == "done":
            prefs = store.get_prefs(chat_id)
            self._edit_or_send(chat_id, message_id, self._filter_text(prefs))
            return "Đã lưu bộ lọc"
        if category == "reset":
            prefs = store.set_pref(chat_id, **{field: PREF_DEFAULTS[field]
                                               for field, _ in kb.FILTER_FIELDS.values()})
            answer = "Đã xóa bộ lọc"
        elif category in kb.FILTER_FIELDS and not value:
            prefs = store.get_prefs(chat_id)
            self._edit_or_send(chat_id, message_id, self._filter_text(prefs),
                               reply_markup=kb.filter_options_keyboard(category, prefs))
            return ""
        elif category in kb.FILTER_FIELDS:
            field, choices = kb.FILTER_FIELDS[category]
            if value not in choices:
                return "Lựa chọn không hợp lệ."
            stored = SALARY_CHOICES[value] if category == "sal" else value
            prefs = store.set_pref(chat_id, **{field: stored})
            answer = f"Đã chọn: {choices[value]}"
        elif category == "home":
            prefs, answer = store.get_prefs(chat_id), ""
        else:
            return "Nút này đã hết hạn."
        self._edit_or_send(chat_id, message_id, self._filter_text(prefs),
                           reply_markup=kb.filter_home_keyboard())
        return answer

    # ── /exclude ─────────────────────────────────────────────────

    def _handle_exclude(self, chat_id: str, argument: str) -> None:
        argument = argument.strip()
        store = self.store
        command, _, value = argument.partition(" ")
        if not argument or command.lower() in {"list", "ds", "danh-sach"}:
            items = store.list_excludes(chat_id)
            if not items:
                text = ("🚫 Danh sách loại trừ đang trống.\n"
                        "Thêm bằng: /exclude bán hàng đa cấp hoặc /exclude Tên Công Ty")
            else:
                listed = "\n".join(f"• {item.get('raw') or item['sk'][8:]}" for item in items)
                text = (f"🚫 Đang loại trừ {len(items)} mục:\n{listed}\n\n"
                        "Bỏ một mục: /exclude remove <từ khóa>")
            self.send_message(chat_id, text, parse_mode="")
            return
        if command.lower() in {"remove", "xoa", "xóa", "del"}:
            if not value.strip():
                self.send_message(chat_id, "Nhập mục cần bỏ, ví dụ: /exclude remove sales",
                                  parse_mode="")
                return
            removed = store.remove_exclude(chat_id, value)
            self.send_message(
                chat_id,
                f"✅ Đã bỏ loại trừ: {value.strip()}" if removed
                else f"Không có “{value.strip()}” trong danh sách. Xem: /exclude list",
                parse_mode="",
            )
            return
        if not normalize_term(argument):
            self.send_message(chat_id, "Từ khóa loại trừ không hợp lệ.", parse_mode="")
            return
        if len(store.list_excludes(chat_id)) >= MAX_EXCLUDES:
            self.send_message(chat_id, f"Tối đa {MAX_EXCLUDES} mục loại trừ. "
                              "Bỏ bớt bằng /exclude remove <từ khóa>.", parse_mode="")
            return
        store.add_exclude(chat_id, argument)
        self.send_message(
            chat_id,
            f"🚫 Sẽ không hiện tin có “{argument}” trong tiêu đề hoặc tên công ty "
            "(tìm kiếm, việc phù hợp và thông báo).",
            parse_mode="",
        )

    # ── /follow and /company ─────────────────────────────────────

    def _handle_follow(self, chat_id: str, argument: str) -> None:
        argument = argument.strip()
        store = self.store
        command, _, value = argument.partition(" ")
        if not argument or command.lower() == "list":
            items = store.list_follows(chat_id)
            if not items:
                text = ("🏢 Bạn chưa theo dõi công ty nào.\n"
                        "Dùng /company <tên> rồi bấm “🔔 Theo dõi công ty”, "
                        "hoặc /follow <tên công ty>.")
            else:
                listed = "\n".join(f"• {item.get('name') or item['sk'][7:]}" for item in items)
                text = (f"🏢 Công ty đang theo dõi:\n{listed}\n\n"
                        "Bỏ theo dõi: /unfollow <tên công ty>")
            self.send_message(chat_id, text, parse_mode="")
            return
        if command.lower() in {"remove", "xoa", "xóa"}:
            self._handle_unfollow(chat_id, value)
            return
        if not normalize_term(argument):
            self.send_message(chat_id, "Tên công ty không hợp lệ.", parse_mode="")
            return
        if len(store.list_follows(chat_id)) >= MAX_FOLLOWS:
            self.send_message(chat_id, f"Tối đa {MAX_FOLLOWS} công ty theo dõi.", parse_mode="")
            return
        store.follow(chat_id, argument)
        self.send_message(chat_id, f"🔔 Đã theo dõi {argument}. Tin mới của công ty này "
                          "sẽ có trong thông báo.", parse_mode="")

    def _handle_unfollow(self, chat_id: str, name: str) -> None:
        if not name.strip():
            self._handle_follow(chat_id, "list")
            return
        removed = self.store.unfollow(chat_id, name)
        self.send_message(
            chat_id,
            f"✅ Đã bỏ theo dõi {name.strip()}" if removed
            else "Không tìm thấy công ty này trong danh sách theo dõi. Xem: /follow",
            parse_mode="",
        )

    def _follow_from_button(self, chat_id: str, message: dict, normalized: str) -> str:
        if not normalized:
            return "Nút này đã hết hạn."
        name = normalized
        first_line = str(message.get("text") or "").split("\n", 1)[0]
        if first_line.startswith("🏢 ") and " · " in first_line:
            candidate = first_line[2:].split(" · ", 1)[0].strip()
            if normalize_term(candidate)[: len(normalized)] == normalized:
                name = candidate
        self.store.follow(chat_id, name)
        return f"🔔 Đã theo dõi {name}"

    def _handle_company(self, chat_id: str, name: str) -> None:
        from src.matcher.search import matches_filters, published_at

        name = name.strip()
        term = normalize_term(name)
        if len(term) < 2:
            self.send_message(chat_id, "Nhập tên công ty, ví dụ: /company FPT Software",
                              parse_mode="")
            return
        temp = self.send_message(chat_id, f"🏢 Đang tìm tin của {name}…", parse_mode="",
                                 reply_markup={"inline_keyboard": []})
        message_id = temp.get("message_id") if temp else None
        try:
            live_future = self._start_live_search(name, None, limit=20)
            items = self.db_loader.get_search_items(deadline_at=self._request_deadline_at)
            database_jobs = [
                job for job in items
                if company_matches(job, term)
                and matches_filters(job, max_age_days=self.settings.max_job_age_days)
            ]
            live_jobs = [job for job in self._collect_live_search(live_future)
                         if company_matches(job, term)]
            jobs = self._merge_unique_jobs(live_jobs, database_jobs)
            ctx = self._load_context(chat_id)
            if ctx is not None:
                from src.bot.filters import apply_context

                jobs = apply_context(jobs, ctx, use_filters=False)
            jobs.sort(key=lambda job: (published_at(job) or _EPOCH).timestamp(), reverse=True)
            follow_key = term[:56]
            if not jobs:
                self._edit_or_send(
                    chat_id, message_id,
                    f"🏢 Chưa thấy tin đang tuyển của {name}. "
                    "Bấm theo dõi để được báo khi công ty đăng tin mới.",
                    reply_markup=kb.inline([kb.follow_button(follow_key)]),
                )
                return
            self._save_search(chat_id, jobs, f"🏢 {name} · việc đang tuyển",
                              follow_company=follow_key)
            self._handle_more(chat_id, message_id)
        except Exception:
            logger.exception("Company search failed")
            self._edit_or_send(chat_id, message_id,
                               "Không tải được dữ liệu lúc này. Vui lòng thử lại sau.")

    # ── Onboarding (/start without subscriptions) ────────────────

    def _start_onboarding(self, chat_id: str, first_name: str = "") -> None:
        greeting = f"Chào {first_name}! " if first_name else "Chào bạn! "
        self.send_message(
            chat_id,
            f"👋 {greeting}Mình là ViecLamBot — tìm việc và báo tin tuyển dụng mới.\n\n"
            "Thiết lập nhanh 3 bước để nhận việc phù hợp:\n"
            "Bước 1/3 — Bạn đang tìm nghề gì?",
            parse_mode="",
            reply_markup=kb.onboarding_keyboard(1),
        )

    def _onboarding_keyword(self, chat_id: str, keyword: str, message_id=None) -> None:
        self.store.set_onboarding(chat_id, keyword=keyword[:120])
        self._edit_or_send(
            chat_id, message_id,
            f"✅ Nghề: {keyword}\n\nBước 2/3 — Bạn muốn làm ở đâu?",
            reply_markup=kb.onboarding_keyboard(2),
        )

    def _onboarding_button(self, chat_id: str, message_id, step: int, value: str) -> str:
        store = self.store
        if step == 1:
            if value == "custom":
                self._set_pending_action(chat_id, "onboard")
                self.send_message(chat_id, "✍️ Nhập tên nghề hoặc kỹ năng, ví dụ: "
                                  "kỹ sư cầu đường", parse_mode="")
                return ""
            keyword = kb.ONBOARD_OCCUPATIONS.get(value)
            if not keyword:
                return "Lựa chọn không hợp lệ."
            self._onboarding_keyword(chat_id, keyword, message_id)
            return ""
        if step == 2:
            if value not in CITY_CHOICES:
                return "Lựa chọn không hợp lệ."
            store.set_onboarding(chat_id, location=value)
            self._edit_or_send(chat_id, message_id,
                               f"✅ Địa điểm: {CITY_CHOICES[value]}\n\n"
                               "Bước 3/3 — Cấp bậc mong muốn?",
                               reply_markup=kb.onboarding_keyboard(3))
            return ""
        if value not in LEVEL_CHOICES:
            return "Lựa chọn không hợp lệ."
        state = store.get_onboarding(chat_id)
        keyword = str(state.get("keyword") or "")
        if not keyword:
            self._start_onboarding(chat_id)
            return "Phiên thiết lập đã hết hạn, hãy chọn lại."
        location_code = str(state.get("location") or "all")
        store.set_pref(chat_id, filter_level=value)
        store.clear_onboarding(chat_id)
        self._edit_or_send(chat_id, message_id,
                           f"✅ Cấp bậc: {LEVEL_CHOICES[value]}\nĐang tạo thông báo…")
        location = CITY_CHOICES.get(location_code) if location_code != "all" else None
        self._handle_subscribe(chat_id, f"{keyword} | {location}" if location else keyword)
        self.send_message(
            chat_id,
            "🎉 Xong! Bấm 🎯 Việc phù hợp để xem việc ngay, hoặc gõ tên nghề để tìm.",
            parse_mode="",
        )
        return "Đã tạo thông báo đầu tiên"

