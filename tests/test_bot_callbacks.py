"""Bot v2: inline job buttons, /saved, /history, /settings, /filter, /exclude,
/company, /follow, onboarding and /trending."""

from html.parser import HTMLParser
from unittest.mock import Mock

import pytest

from src.bot import keyboards as kb
from src.bot.filters import (
    UserContext,
    apply_context,
    detect_level,
    is_excluded,
    job_type_ok,
    level_ok,
)
from src.bot.handler import MENU_SAVED, TelegramBot, _processed_update_ids, main_menu_markup
from src.bot.messages import job_pages
from src.bot.store import UserStore
from src.matcher.search import identity

_update_ids = iter(range(5000, 100000))


class _TelegramText(HTMLParser):
    """Render bot HTML the way Telegram hands it back: plain text + entities."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.entities, self._href, self._start = "", [], None, 0

    @staticmethod
    def _len(text):
        return len(text.encode("utf-16-le")) // 2

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._start = dict(attrs)["href"], self._len(self.text)

    def handle_endtag(self, tag):
        if tag == "a":
            self.entities.append({"type": "text_link", "offset": self._start,
                                  "length": self._len(self.text) - self._start,
                                  "url": self._href})

    def handle_data(self, data):
        self.text += data


def telegram_message(html: str, message_id: int = 77) -> dict:
    parser = _TelegramText()
    parser.feed(html)
    return {"message_id": message_id, "chat": {"id": 123}, "text": parser.text,
            "entities": parser.entities}


@pytest.fixture
def bot(database, monkeypatch):
    _processed_update_ids.clear()
    bot = TelegramBot()
    bot.settings.you_search_enabled = False
    bot.settings.live_search_enabled = False
    monkeypatch.setattr(bot, "send_message", Mock(return_value={"message_id": 77}))
    monkeypatch.setattr(bot, "edit_message", Mock(return_value={"message_id": 77}))
    monkeypatch.setattr(bot, "answer_callback", Mock(return_value=True))
    return bot


def press(bot, data: str, message: dict | None = None):
    bot.handle_webhook({
        "update_id": next(_update_ids),
        "callback_query": {"id": "cb", "from": {"id": 123}, "data": data,
                           "message": message or {"message_id": 77, "chat": {"id": 123}}},
    })


def say(bot, text: str):
    bot.handle_webhook({"update_id": next(_update_ids),
                        "message": {"chat": {"id": 123}, "from": {"first_name": "An"},
                                    "text": text}})


def item(database, sk):
    return database[1].get_item(Key={"user_id": "123", "sk": sk}).get("Item")


def two_jobs(make_job):
    return [
        make_job(job_id="a", title="Kế toán tổng hợp", company="Alpha & Co",
                 source_url="https://example.com/a?x=1&y=2"),
        make_job(job_id="b", title="Data Engineer", company="Beta",
                 source_url="https://example.com/b"),
    ]


# ── Buttons under every card ────────────────────────────────────────


def test_every_card_gets_buttons_with_short_callback_data(bot, make_job):
    jobs = [make_job(job_id=str(i), company=f"C{i}" * 40) for i in range(7)]
    bot._save_search("123", jobs, "Jobs")
    bot._handle_more("123")
    markup = bot.send_message.call_args.kwargs["reply_markup"]
    rows = markup["inline_keyboard"]
    assert len(rows) == 6  # five cards + "Xem thêm"
    assert [b["text"] for b in rows[0]] == ["💾 1", "🙈 1", "👍 1", "👎 1"]
    assert rows[-1][0]["callback_data"] == "m"
    for row in rows:
        for button in row:
            assert len(button["callback_data"].encode("utf-8")) <= 64
    assert rows[1][0]["callback_data"] == f"j:s:2:{identity(jobs[1])}"


def test_parse_card_recovers_fields_from_callback_message(make_job):
    jobs = two_jobs(make_job)
    html, included = next(job_pages(jobs, "🔔 Việc mới: kế toán"))
    message = telegram_message("📬 Hôm nay có 2 tin mới cho 1 đăng ký\n\n" + html)
    card = kb.parse_card(message, 1)
    assert card["title"] == "Kế toán tổng hợp"
    assert card["company"] == "Alpha & Co"
    assert card["source_url"] == "https://example.com/a?x=1&y=2"
    assert kb.parse_card(message, 2)["source_url"] == "https://example.com/b"
    assert kb.parse_card(message, 9) is None


def test_save_button_stores_card_and_answers(bot, database, make_job):
    jobs = two_jobs(make_job)
    html, _ = next(job_pages(jobs, "Jobs"))
    key = identity(jobs[1])
    press(bot, f"j:s:2:{key}", telegram_message(html))
    saved = item(database, f"SAVED#{key}")
    assert saved["title"] == "Data Engineer"
    assert saved["company"] == "Beta"
    assert saved["source_url"] == "https://example.com/b"
    assert saved["saved_at"]
    bot.answer_callback.assert_called_once()
    assert "Đã lưu" in bot.answer_callback.call_args.args[1]


def test_save_button_falls_back_to_sent_payload(bot, database, make_job):
    from src.config import get_settings
    from src.matcher.alerts import DeliveryStore

    job = make_job(title="Kỹ sư cầu đường")
    DeliveryStore(get_settings()).mark_sent("123", [job])
    press(bot, f"j:s:1:{identity(job)}")
    assert item(database, f"SAVED#{identity(job)}")["title"] == "Kỹ sư cầu đường"


def test_hide_and_dislike_buttons(bot, database, make_job):
    jobs = two_jobs(make_job)
    html, _ = next(job_pages(jobs, "Jobs"))
    press(bot, f"j:h:1:{identity(jobs[0])}", telegram_message(html))
    assert item(database, f"HIDDEN#{identity(jobs[0])}")["reason"] == "hide"
    press(bot, f"j:d:2:{identity(jobs[1])}", telegram_message(html))
    assert item(database, f"HIDDEN#{identity(jobs[1])}")["reason"] == "down"
    feedback = item(database, f"FEEDBACK#{identity(jobs[1])}")
    assert feedback["vote"] == "down" and feedback["company_normalized"] == "beta"
    press(bot, f"j:u:1:{identity(jobs[0])}", telegram_message(html))
    assert item(database, f"FEEDBACK#{identity(jobs[0])}")["vote"] == "up"
    ctx = UserStore().load_context("123")
    assert ctx.hidden == {identity(jobs[0]), identity(jobs[1])}
    assert ctx.disliked_companies == {"beta"}
    assert bot.answer_callback.call_count == 3


def test_callback_is_answered_even_when_handler_fails(bot, monkeypatch):
    monkeypatch.setattr(bot, "_job_button", Mock(side_effect=RuntimeError("boom")))
    press(bot, "j:s:1:" + "a" * 32)
    bot.answer_callback.assert_called_once()
    assert "lỗi" in bot.answer_callback.call_args.args[1]


def test_unknown_callback_is_answered(bot):
    press(bot, "zz:1")
    assert "hết hạn" in bot.answer_callback.call_args.args[1]


def test_more_button_sends_next_page(bot, make_job):
    bot._save_search("123", [make_job(company=str(i)) for i in range(7)], "Jobs")
    bot._handle_more("123")
    press(bot, "m")
    assert "6–7/7" in bot.send_message.call_args.args[1]
    bot.answer_callback.assert_called_once()


def test_hidden_and_excluded_jobs_never_return_in_search(bot, database, make_job, monkeypatch):
    jobs = [
        make_job(job_id="1", title="Data Engineer", company="Hidden Corp"),
        make_job(job_id="2", title="Data Engineer", company="Đa Cấp Group"),
        make_job(job_id="3", title="Data Engineer", company="Disliked Ltd"),
        make_job(job_id="4", title="Data Engineer", company="Good Co"),
    ]
    store = UserStore()
    store.hide_job("123", identity(jobs[0]))
    store.add_exclude("123", "da cap")
    store.feedback("123", "f" * 32, "down", {"company": "Disliked Ltd"})
    monkeypatch.setattr(bot.db_loader, "search_jobs", Mock(return_value=jobs))
    save = Mock()
    monkeypatch.setattr(bot, "_save_search", save)
    monkeypatch.setattr(bot, "_handle_more", Mock())
    bot._handle_search("123", "data engineer")
    assert [job["company"] for job in save.call_args.args[1]] == ["Good Co", "Disliked Ltd"]


def test_myjobs_applies_hidden_and_filter_defaults(bot, database, make_job, monkeypatch):
    from src.common.models import Subscription

    database[1].put_item(Item=Subscription(user_id="123", keyword_raw="data").to_dynamo_item())
    jobs = [make_job(job_id="1", title="Senior Data Engineer", company="A"),
            make_job(job_id="2", title="Data Engineer Intern", company="B")]
    store = UserStore()
    store.set_pref("123", filter_level="intern", filter_location="hn",
                   filter_salary_min=10_000_000)
    search = Mock(return_value=jobs)
    monkeypatch.setattr(bot.db_loader, "search_jobs", search)
    save = Mock()
    monkeypatch.setattr(bot, "_save_search", save)
    monkeypatch.setattr(bot, "_handle_more", Mock())
    bot._handle_myjobs("123")
    assert search.call_args.kwargs["location"] == "Hà Nội"
    assert search.call_args.kwargs["salary_min"] == 10_000_000
    assert [job["company"] for job in save.call_args.args[1]] == ["B"]


# ── /saved and /history ─────────────────────────────────────────────


def test_saved_lists_bookmarks_with_unsave_buttons(bot, database, make_job):
    store = UserStore()
    for number in range(7):
        store.save_job("123", f"{number:032d}", make_job(title=f"Job {number}"))
    say(bot, MENU_SAVED)
    text = bot.send_message.call_args.args[1]
    assert "📌 Tin đã lưu (7)" in text and "Job" in text
    rows = bot.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["callback_data"].startswith("j:r:1:")
    press(bot, rows[0][0]["callback_data"])
    assert len(store.list_saved("123")) == 6


def test_saved_empty(bot):
    say(bot, "/saved")
    assert "chưa lưu" in bot.send_message.call_args.args[1]


def test_history_shows_jobs_sent_by_alerts(bot, database, make_job):
    from src.config import get_settings
    from src.matcher.alerts import DeliveryStore

    DeliveryStore(get_settings()).mark_sent("123", [make_job(title="Kế toán trưởng")])
    database[1].put_item(Item={"user_id": "123", "sk": "SENT#old", "sent_at": "2020-01-01",
                               "ttl": 9999999999, "title": "Old"})
    say(bot, "/history")
    text = bot.send_message.call_args.args[1]
    assert "Kế toán trưởng" in text and "Old" not in text and "7 ngày" in text


# ── /settings and /filter ───────────────────────────────────────────


def test_settings_buttons_update_pref(bot, database):
    say(bot, "/settings")
    markup = bot.send_message.call_args.kwargs["reply_markup"]
    datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert {"st:fq:daily", "st:fq:weekly", "st:qh", "st:mx:5", "st:lg"} <= set(datas)
    press(bot, "st:fq:daily")
    press(bot, "st:qh")
    press(bot, "st:mx:5")
    pref = item(database, "PREF")
    assert pref["frequency"] == "daily" and pref["quiet_hours"] is True
    assert pref["max_jobs"] == 5
    assert "1 lần/ngày" in bot.edit_message.call_args.args[2]
    press(bot, "st:lg")
    assert "tiếng Việt" in bot.answer_callback.call_args.args[1]


def test_filter_wizard_sets_defaults(bot, database):
    say(bot, "/filter")
    assert "Bộ lọc" in bot.send_message.call_args.args[1]
    press(bot, "f:loc")
    options = bot.edit_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
    assert any(b["callback_data"] == "f:loc:hcm" for row in options for b in row)
    press(bot, "f:loc:hcm")
    press(bot, "f:sal:15")
    press(bot, "f:lvl:junior")
    press(bot, "f:typ:remote")
    pref = item(database, "PREF")
    assert (pref["filter_location"], pref["filter_salary_min"]) == ("hcm", 15_000_000)
    assert (pref["filter_level"], pref["filter_job_type"]) == ("junior", "remote")
    assert "từ 15 triệu" in bot.edit_message.call_args.args[2]
    press(bot, "f:reset")
    assert item(database, "PREF")["filter_location"] == "all"


def test_search_uses_filter_location_unless_query_names_one(bot, database, make_job,
                                                           monkeypatch):
    UserStore().set_pref("123", filter_location="dn")
    merge = Mock(return_value=[])
    monkeypatch.setattr(bot, "_merge_ranked_jobs", merge)
    monkeypatch.setattr(bot.db_loader, "search_jobs", Mock(return_value=[]))
    bot._handle_search("123", "kế toán")
    assert merge.call_args.args[1] == "Đà Nẵng"
    bot._handle_search("123", "kế toán | Hà Nội")
    assert merge.call_args.args[1] == "Hà Nội"


def test_new_subscription_inherits_filter_defaults(bot, database):
    UserStore().set_pref("123", filter_location="hn", filter_salary_min=20_000_000)
    say(bot, "/subscribe python")
    sub = item(database, "SUB#python")
    assert sub["location_filter"] == "ha noi" and sub["salary_min_filter"] == 20_000_000


def test_level_and_job_type_heuristics():
    assert detect_level({"title": "Thực tập sinh Kế toán"}) == "intern"
    assert detect_level({"title": "Senior Backend Developer"}) == "senior"
    assert detect_level({"title": "Trưởng nhóm kinh doanh"}) == "lead"
    assert detect_level({"title": "Fresher Java"}) == "fresher"
    assert detect_level({"title": "Kế toán viên"}) is None
    assert level_ok({"title": "Team Lead"}, "senior")
    assert not level_ok({"title": "Kế toán viên"}, "intern")
    assert level_ok({"title": "Kế toán viên"}, "junior")
    assert not level_ok({"title": "Senior Dev"}, "fresher")
    assert job_type_ok({"title": "Nhân viên bán thời gian"}, "pt")
    assert not job_type_ok({"title": "Nhân viên part-time"}, "ft")
    assert job_type_ok({"title": "Python dev", "location": "Remote"}, "remote")
    assert job_type_ok({"title": "Python dev", "job_type": "remote"}, "remote")
    assert not job_type_ok({"title": "Python dev"}, "remote")


def test_exclusion_is_accent_and_case_insensitive():
    assert is_excluded({"title": "Nhân viên BÁN HÀNG ĐA CẤP"}, ["da cap"])
    assert is_excluded({"title": "Kế toán", "company": "Công ty ABC"}, ["abc"])
    assert not is_excluded({"title": "Kế toán", "company": "ABCD"}, ["abc"])


def test_apply_context_salary_floor_keeps_unknown_salary(make_job):
    ctx = UserContext(prefs={"filter_salary_min": 15_000_000})
    jobs = [make_job(job_id="1", company="A", salary_max=10_000_000),
            make_job(job_id="2", company="B"),
            make_job(job_id="3", company="C", salary_min=20_000_000)]
    assert [job["company"] for job in apply_context(jobs, ctx)] == ["B", "C"]


# ── /exclude ────────────────────────────────────────────────────────


def test_exclude_add_list_remove(bot, database):
    say(bot, "/exclude Bán hàng đa cấp")
    assert item(database, "EXCLUDE#ban hang da cap")["raw"] == "Bán hàng đa cấp"
    say(bot, "/exclude list")
    assert "Bán hàng đa cấp" in bot.send_message.call_args.args[1]
    say(bot, "/exclude remove ban hang da cap")
    assert "Đã bỏ" in bot.send_message.call_args.args[1]
    assert item(database, "EXCLUDE#ban hang da cap") is None
    say(bot, "/exclude")
    assert "trống" in bot.send_message.call_args.args[1]


# ── /company and /follow ────────────────────────────────────────────


def test_company_lists_postings_and_follow_button(bot, database, make_job, monkeypatch):
    items = [make_job(job_id="1", title="Kế toán", company="Công ty FPT Software"),
             make_job(job_id="2", title="Tester", company="FPT Software"),
             make_job(job_id="3", title="Dev", company="Other")]
    monkeypatch.setattr(bot.db_loader, "get_search_items", Mock(return_value=items))
    say(bot, "/company FPT Software")
    text = bot.edit_message.call_args.args[2]
    assert "Kế toán" in text and "Tester" in text and "Dev" not in text
    rows = bot.edit_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
    follow = rows[-1][0]
    assert follow["text"] == "🔔 Theo dõi công ty"
    press(bot, follow["callback_data"], telegram_message(text))
    assert item(database, "FOLLOW#fpt software")["name"] == "FPT Software"
    say(bot, "/follow")
    assert "FPT Software" in bot.send_message.call_args.args[1]
    say(bot, "/unfollow fpt software")
    assert item(database, "FOLLOW#fpt software") is None


def test_company_without_postings_still_offers_follow(bot, monkeypatch):
    monkeypatch.setattr(bot.db_loader, "get_search_items", Mock(return_value=[]))
    say(bot, "/company Vinamilk")
    markup = bot.edit_message.call_args.kwargs["reply_markup"]
    assert markup["inline_keyboard"][0][0]["callback_data"] == "fo:vinamilk"


# ── Onboarding and menu ─────────────────────────────────────────────


def test_start_onboarding_creates_first_subscription(bot, database):
    say(bot, "/start")
    markup = bot.send_message.call_args.kwargs["reply_markup"]
    assert any(b["callback_data"] == "o1:ketoan" for row in markup["inline_keyboard"]
               for b in row)
    press(bot, "o1:ketoan")
    assert "Bước 2/3" in bot.edit_message.call_args.args[2]
    press(bot, "o2:hn")
    assert "Bước 3/3" in bot.edit_message.call_args.args[2]
    press(bot, "o3:fresher")
    sub = item(database, "SUB#kế toán")
    assert sub["location_filter"] == "ha noi"
    assert item(database, "PREF")["filter_level"] == "fresher"
    assert item(database, "BOT#ONBOARD") is None


def test_onboarding_custom_occupation(bot, database):
    press(bot, "o1:custom")
    say(bot, "kỹ sư cầu đường")
    assert item(database, "BOT#ONBOARD")["keyword"] == "kỹ sư cầu đường"
    press(bot, "o2:all")
    press(bot, "o3:any")
    assert item(database, "SUB#kỹ sư cầu đường")["location_filter"] == ""


def test_existing_user_start_skips_onboarding(bot, database):
    from src.common.models import Subscription

    database[1].put_item(Item=Subscription(user_id="123", keyword_raw="python").to_dynamo_item())
    say(bot, "/start")
    assert bot.send_message.call_count == 1
    assert "ViecLamBot" in bot.send_message.call_args.args[1]


def test_reply_keyboard_has_six_buttons():
    labels = [b["text"] for row in main_menu_markup()["keyboard"] for b in row]
    assert labels == ["🔎 Tìm việc", "🎯 Việc phù hợp", "🔔 Tạo thông báo", "📌 Đã lưu",
                      "⚙️ Cài đặt", "❓ Hướng dẫn"]


def test_settings_shortcuts_open_commands(bot, database):
    press(bot, "go:exclude")
    assert "loại trừ" in bot.send_message.call_args.args[1].lower()


def test_menu_script_lists_new_commands():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "configure_telegram_menu.py"
    spec = importlib.util.spec_from_file_location("configure_telegram_menu", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = {item["command"] for item in module.COMMANDS}
    assert {"saved", "history", "settings", "filter", "exclude", "company",
            "trending"} <= names
    assert all(len(item["description"]) <= 256 for item in module.COMMANDS)


# ── /trending ───────────────────────────────────────────────────────


def test_trending_sends_formatted_summary(bot, monkeypatch):
    summary = Mock(return_value={"jobs": 10})
    monkeypatch.setattr("src.analytics.trending.trending_summary", summary)
    monkeypatch.setattr("src.analytics.trending.format_trending",
                        Mock(return_value="<b>📈 Xu hướng</b>"))
    say(bot, "/trending Hà Nội")
    summary.assert_called_once_with(days=7, city="Hà Nội")
    assert bot.send_message.call_args.args[1] == "<b>📈 Xu hướng</b>"
    assert bot.send_message.call_args.kwargs["parse_mode"] == "HTML"


def test_trending_failure_replies_with_short_error(bot, monkeypatch):
    monkeypatch.setattr("src.analytics.trending.trending_summary",
                        Mock(side_effect=RuntimeError("scan failed")))
    say(bot, "/trending")
    assert "Chưa tải được thống kê" in bot.send_message.call_args.args[1]
