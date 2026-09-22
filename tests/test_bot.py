from unittest.mock import Mock

from src.bot.handler import (
    MENU_SEARCH,
    MENU_WEB_SEARCH,
    TelegramBot,
    _processed_update_ids,
)
from src.bot.messages import job_pages


def test_long_cards_and_untrusted_html(make_job):
    jobs = [
        make_job(
            title="<b>&😀" * 300,
            company="<script>" * 100,
            source_url="https://example.com/?q=" + "&" * 800,
        )
        for _ in range(20)
    ]
    pages = list(job_pages(jobs, "<heading>"))
    assert sum(len(included) for _, included in pages) == 20
    for text, _ in pages:
        assert len(text.encode("utf-16-le")) // 2 <= 3500
        assert "<script>" not in text
        assert "&lt;heading&gt;" in text


def test_pagination_continues_and_failed_send_preserves_offset(database, make_job, monkeypatch):
    bot = TelegramBot()
    bot._save_search("123", [make_job(company=str(i)) for i in range(12)], "Jobs")
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr(bot, "send_message", send)
    bot._handle_more("123")
    state = database[1].get_item(Key={"user_id": "123", "sk": "SEARCH#LAST"})["Item"]
    assert state["offset"] == 5
    send.return_value = None
    bot._handle_more("123")
    assert database[1].get_item(Key={"user_id": "123", "sk": "SEARCH#LAST"})["Item"]["offset"] == 5
    send.return_value = {"message_id": 2}
    bot._handle_more("123")
    assert "6–10/12" in send.call_args.args[1]


def test_does_not_retry_forbidden_as_markdown_error(monkeypatch):
    post = Mock(return_value=Mock(status_code=403, text="Forbidden"))
    monkeypatch.setattr("src.bot.handler.requests.post", post)
    assert TelegramBot().send_message("123", "hello") is None
    assert post.call_count == 1


def test_rate_limit_obeys_retry_after(monkeypatch):
    limited = Mock(status_code=429, text="Too Many Requests")
    limited.json.return_value = {"parameters": {"retry_after": 1}}
    okay = Mock(status_code=200)
    okay.json.return_value = {"result": {"message_id": 7}}
    post = Mock(side_effect=[limited, okay])
    sleep = Mock()
    monkeypatch.setattr("src.bot.handler.requests.post", post)
    monkeypatch.setattr("src.bot.handler.time.sleep", sleep)
    assert TelegramBot().send_message("123", "hello")["message_id"] == 7
    sleep.assert_called_once_with(1)


def test_messages_include_persistent_main_menu(monkeypatch):
    okay = Mock(status_code=200)
    okay.json.return_value = {"result": {"message_id": 8}}
    post = Mock(return_value=okay)
    monkeypatch.setattr("src.bot.handler.requests.post", post)

    TelegramBot().send_message("123", "hello", parse_mode="")

    markup = post.call_args.kwargs["json"]["reply_markup"]
    assert markup["is_persistent"] is True
    assert markup["resize_keyboard"] is True
    assert markup["keyboard"][0][0]["text"] == MENU_SEARCH


def test_search_menu_button_sets_conversation_state(database, monkeypatch):
    _processed_update_ids.clear()
    bot = TelegramBot()
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr(bot, "send_message", send)

    bot.handle_webhook(
        {
            "update_id": 101,
            "message": {"chat": {"id": 123}, "from": {}, "text": MENU_SEARCH},
        }
    )

    state = database[1].get_item(Key={"user_id": "123", "sk": "BOT#STATE"})["Item"]
    assert state["action"] == "search"
    assert "nhập nghề" in send.call_args.args[1]


def test_web_search_menu_button_sets_conversation_state(database, monkeypatch):
    _processed_update_ids.clear()
    bot = TelegramBot()
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr(bot, "send_message", send)

    bot.handle_webhook(
        {
            "update_id": 102,
            "message": {"chat": {"id": 123}, "from": {}, "text": MENU_WEB_SEARCH},
        }
    )

    state = database[1].get_item(Key={"user_id": "123", "sk": "BOT#STATE"})["Item"]
    assert state["action"] == "web_search"
    assert "Internet" in send.call_args.args[1]


def test_explicit_web_search_saves_verified_results(database, make_job, monkeypatch):
    bot = TelegramBot()
    bot.settings.you_search_enabled = True
    web_job = make_job(
        source="Web · Acme",
        source_url="https://acme.example/careers/data",
    )
    monkeypatch.setattr(bot, "_search_web", Mock(return_value=[web_job]))
    save = Mock()
    more = Mock()
    monkeypatch.setattr(bot, "_save_search", save)
    monkeypatch.setattr(bot, "_handle_more", more)
    monkeypatch.setattr(bot, "send_message", Mock(return_value={"message_id": 9}))

    bot._handle_web_search("123", "data engineer | HCM")

    save.assert_called_once()
    assert save.call_args.args[1] == [web_job]
    assert "Kết quả web" in save.call_args.args[2]
    more.assert_called_once_with("123", 9)


def test_normal_search_puts_recent_web_results_before_database(
    database, make_job, monkeypatch
):
    bot = TelegramBot()
    bot.settings.you_search_enabled = True
    web_job = make_job(
        job_id="web-job",
        company="New Web Employer",
        source="Web · TopCV",
        source_url="https://topcv.vn/viec-lam/new-job/1",
        is_web_result=True,
    )
    database_job = make_job(
        job_id="database-job",
        company="Existing Employer",
        source_url="https://example.com/jobs/database-job",
    )
    monkeypatch.setattr(bot, "_search_web", Mock(return_value=[web_job]))
    monkeypatch.setattr(
        bot.db_loader, "search_jobs", Mock(return_value=[database_job])
    )
    save = Mock()
    more = Mock()
    monkeypatch.setattr(bot, "_save_search", save)
    monkeypatch.setattr(bot, "_handle_more", more)
    monkeypatch.setattr(bot, "send_message", Mock(return_value={"message_id": 10}))

    bot._handle_search("123", "data engineer | HCM")

    assert save.call_args.args[1] == [web_job, database_job]
    assert "web mới trước" in save.call_args.args[2]
    more.assert_called_once_with("123", 10)


def test_persistent_update_claim_blocks_retry_in_new_container(database, monkeypatch):
    _processed_update_ids.clear()
    first = TelegramBot()
    first_search = Mock()
    monkeypatch.setattr(first, "_handle_search", first_search)
    body = {
        "update_id": 202,
        "message": {"chat": {"id": 123}, "from": {}, "text": "data analyst"},
    }
    first.handle_webhook(body)
    assert first_search.call_count == 1

    # Simulate a cold Lambda container: RAM state is gone, DynamoDB remains.
    _processed_update_ids.clear()
    second = TelegramBot()
    second_search = Mock()
    monkeypatch.setattr(second, "_handle_search", second_search)
    second.handle_webhook(body)
    assert second_search.call_count == 0
