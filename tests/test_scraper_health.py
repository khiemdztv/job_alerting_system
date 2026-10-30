from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.common import scraper_health as sh
from src.common.scraper_health import ScrapeResult, ScrapeStatus, record_health
from src.config import get_settings


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(
        "src.bot.handler.TelegramBot.send_message",
        lambda self, chat_id, text, parse_mode="MarkdownV2", **kw: messages.append(
            (chat_id, text, parse_mode)
        ),
    )
    return messages


@pytest.fixture
def settings(database):
    base = get_settings()
    return SimpleNamespace(
        aws_region=base.aws_region,
        dynamodb_users_table=base.dynamodb_users_table,
        admin_chat_id="42",
    )


def _run(settings, status, jobs=0, error=""):
    record_health(ScrapeResult("careerlink", "kw", status, jobs, error), settings)


def _item(users):
    return users.get_item(Key={"user_id": "HEALTH#careerlink", "sk": "STATUS"})["Item"]


def _count(sent, needle):
    return len([m for m in sent if needle in m[1]])


def test_pauses_after_five_bad_runs_and_alerts_once(database, settings, sent):
    _, users = database
    for _ in range(4):
        _run(settings, ScrapeStatus.BLOCKED, error="HTTP403")
    assert not sh.is_paused("careerlink", settings)
    _run(settings, ScrapeStatus.ERROR, error="HTTP403")
    assert sh.is_paused("careerlink", settings)
    assert _item(users)["paused_until"]
    assert _count(sent, "tạm dừng") == 1
    assert [m for m in sent if "tạm dừng" in m[1]][0][2] == "HTML"
    _run(settings, ScrapeStatus.BLOCKED, error="HTTP403")  # re-pause: no second message
    assert _count(sent, "tạm dừng") == 1


def test_pause_expires_and_recovery_alert(database, settings, sent):
    for _ in range(5):
        _run(settings, ScrapeStatus.LAYOUT_CHANGED, error="layout_changed")
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert not sh.is_paused("careerlink", settings, now=later)
    _run(settings, ScrapeStatus.OK, jobs=12)
    assert _count(sent, "hoạt động lại") == 1
    assert not sh.is_paused("careerlink", settings)


def test_empty_run_breaks_the_streak(database, settings, sent):
    for status in [ScrapeStatus.ERROR] * 3 + [ScrapeStatus.EMPTY] + [ScrapeStatus.ERROR] * 4:
        _run(settings, status, error="x")
    assert not sh.is_paused("careerlink", settings)


def test_history_is_bounded_and_limited_to_seven_days(database, settings, sent):
    _, users = database
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    users.put_item(Item={
        "user_id": "HEALTH#careerlink", "sk": "STATUS",
        "history": [{"t": old, "s": "ok", "n": 1, "e": ""}]
        + [{"t": now, "s": "ok", "n": 1, "e": ""}] * 80,
    })
    _run(settings, ScrapeStatus.OK, jobs=3)
    history = _item(users)["history"]
    assert len(history) == sh.HISTORY_MAX and all(h["t"] != old for h in history)


def test_no_admin_means_no_messages(database, settings, sent):
    settings.admin_chat_id = None
    for _ in range(5):
        _run(settings, ScrapeStatus.BLOCKED, error="HTTP403")
    assert sent == []
    assert sh.is_paused("careerlink", settings)


def test_weekly_summary_only_monday_morning_and_once(database, settings, sent):
    _run(settings, ScrapeStatus.OK, jobs=10)
    _run(settings, ScrapeStatus.BLOCKED, error="HTTP403")
    vn = timezone(timedelta(hours=7))
    monday = datetime(2026, 11, 2, 6, 0, tzinfo=vn)
    assert monday.weekday() == 0
    assert not sh.maybe_send_weekly_summary(settings, monday.replace(hour=9))
    assert not sh.maybe_send_weekly_summary(settings, monday + timedelta(days=1))
    assert sh.maybe_send_weekly_summary(settings, monday)
    assert not sh.maybe_send_weekly_summary(settings, monday + timedelta(hours=1))
    text = sent[-1][1]
    assert "careerlink" in text and "2 lần" in text and "50% ok" in text and "HTTP403" in text


def test_weekly_summary_skipped_without_admin(database, settings, sent):
    settings.admin_chat_id = ""
    monday = datetime(2026, 11, 2, 6, 0, tzinfo=timezone(timedelta(hours=7)))
    assert not sh.maybe_send_weekly_summary(settings, monday)
    assert sent == []
