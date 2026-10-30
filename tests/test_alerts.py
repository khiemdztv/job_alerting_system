from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src.common.models import Subscription
from src.config import get_settings
from src.matcher.alerts import DeliveryStore, run_alerts
from src.matcher.search import identity


def populate(database, make_job, count=12):
    jobs, users = database
    users.put_item(
        Item=Subscription(
            user_id="123", keyword_raw="data engineer", location_filter="HCM"
        ).to_dynamo_item()
    )
    for number in range(count):
        jobs.put_item(Item=make_job(company=f"Company {number}", job_id=str(number)))


def test_alert_pages_and_retry_skip_delivered(database, make_job, monkeypatch):
    populate(database, make_job)
    send = Mock(side_effect=[{"message_id": 1}, None])
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    with pytest.raises(RuntimeError, match="incomplete"):
        run_alerts({}, None)
    store = DeliveryStore(get_settings())
    assert len(store.sent_ids("123")) == 5
    send.side_effect = None
    send.return_value = {"message_id": 2}
    summary = run_alerts({}, None)
    assert summary["total_matches"] == 7
    assert len(store.sent_ids("123")) == 12
    assert run_alerts({}, None)["notifications_sent"] == 0


def test_recovery_after_missed_schedule(database, make_job, monkeypatch):
    jobs, users = database
    users.put_item(Item=Subscription(user_id="123", keyword_raw="data engineer").to_dynamo_item())
    jobs.put_item(
        Item=make_job(scraped_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
    )
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    assert run_alerts({}, None)["notifications_sent"] == 1


def test_dry_run_has_no_writes_or_messages(database, make_job, monkeypatch):
    populate(database, make_job, count=1)
    send = Mock()
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    before = database[1].scan()["Items"]
    assert run_alerts({"dry_run": True}, None)["total_matches"] == 1
    assert database[1].scan()["Items"] == before
    send.assert_not_called()


def test_overlapping_subscriptions_only_send_once(database, make_job, monkeypatch):
    populate(database, make_job, count=1)
    database[1].put_item(
        Item=Subscription(user_id="123", keyword_raw="kỹ sư dữ liệu").to_dynamo_item()
    )
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    assert run_alerts({}, None)["notifications_sent"] == 1


def test_lease_prevents_concurrent_delivery(database):
    store = DeliveryStore(get_settings())
    assert store.acquire("123", "owner-a")
    assert not store.acquire("123", "owner-b")
    store.release("123", "owner-a")
    assert store.acquire("123", "owner-b")


def test_old_sent_records_do_not_block_new_delivery(database, make_job):
    database[1].put_item(Item={"user_id": "123", "sk": "SENT#" + identity(make_job()), "ttl": 1})
    assert not DeliveryStore(get_settings()).sent_ids("123")


def test_recovery_is_bounded_and_keeps_unsent_jobs(database, make_job, monkeypatch):
    populate(database, make_job, count=25)
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    first = run_alerts({}, None)
    assert first["notifications_sent"] == 3
    assert first["deferred_jobs"] == 10
    assert len(DeliveryStore(get_settings()).sent_ids("123")) == 15
    assert run_alerts({}, None)["total_matches"] == 10


# ── Bot v2: preferences, schedule, filters, follows, digest header ────────

VN = timezone(timedelta(hours=7))


def vn_time(hour, weekday=None):
    """A VN wall-clock time near today (on the given weekday if asked)."""
    day = datetime.now(VN).date()
    if weekday is not None:
        day -= timedelta(days=(day.weekday() - weekday) % 7)
    return datetime(day.year, day.month, day.day, hour, tzinfo=VN)


def at(monkeypatch, moment):
    monkeypatch.setattr("src.matcher.alerts._utcnow", lambda: moment.astimezone(timezone.utc))


def set_pref(**values):
    from src.bot.store import UserStore

    UserStore().set_pref("123", **values)


def test_schedule_slots_and_quiet_hours():
    from src.bot.schedule import is_due

    daily = {"frequency": "daily"}
    assert is_due(daily, vn_time(7), 6)  # closest run to 08:00
    assert not is_due({**daily, "last_alert_at": vn_time(7).isoformat()}, vn_time(13), 6)
    assert is_due({**daily, "last_alert_at": (vn_time(7) - timedelta(days=1)).isoformat()},
                  vn_time(7), 6)
    # Missed morning (quiet hours/failure) is delivered by the next run, not dropped.
    yesterday = (vn_time(7) - timedelta(days=1)).isoformat()
    assert is_due({**daily, "last_alert_at": yesterday}, vn_time(13), 6)
    weekly = {"frequency": "weekly", "last_alert_at": (vn_time(7, 0) - timedelta(days=6)).isoformat()}
    assert not is_due(weekly, vn_time(7, 0) - timedelta(days=1), 6)
    assert is_due(weekly, vn_time(7, 0) + timedelta(days=7), 6)
    twice = {"frequency": "twice", "last_alert_at": vn_time(7).isoformat()}
    assert not is_due(twice, vn_time(13), 6)
    assert is_due(twice, vn_time(19), 6)
    assert not is_due({"quiet_hours": True}, vn_time(23), 6)
    assert not is_due({"quiet_hours": True}, vn_time(3), 6)
    assert is_due({"quiet_hours": True}, vn_time(9), 6)
    assert is_due({}, vn_time(23), 6)


def test_daily_frequency_waits_for_morning_run(database, make_job, monkeypatch):
    populate(database, make_job, count=3)
    set_pref(frequency="daily", last_alert_at=(vn_time(7) - timedelta(days=1)).isoformat())
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    at(monkeypatch, vn_time(1))
    assert run_alerts({}, None)["scheduled_skips"] == 1
    send.assert_not_called()
    at(monkeypatch, vn_time(7))
    assert run_alerts({}, None)["notifications_sent"] == 1
    at(monkeypatch, vn_time(13))
    database[0].put_item(Item=make_job(company="Later Co", job_id="later"))
    assert run_alerts({}, None)["scheduled_skips"] == 1
    assert send.call_count == 1


def test_quiet_hours_defer_then_deliver(database, make_job, monkeypatch):
    populate(database, make_job, count=2)
    set_pref(quiet_hours=True)
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    at(monkeypatch, vn_time(1))
    run_alerts({}, None)
    send.assert_not_called()
    at(monkeypatch, vn_time(7))
    assert run_alerts({}, None)["notifications_sent"] == 1
    assert len(DeliveryStore(get_settings()).sent_ids("123")) == 2


def test_max_jobs_preference_and_digest_header_and_buttons(database, make_job, monkeypatch):
    populate(database, make_job, count=8)
    set_pref(max_jobs=5)
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    summary = run_alerts({}, None)
    assert summary["notifications_sent"] == 1 and summary["deferred_jobs"] == 3
    text = send.call_args.args[1]
    assert text.startswith("📬 <b>Hôm nay có 8 tin mới cho 1 đăng ký</b>")
    rows = send.call_args.kwargs["reply_markup"]["inline_keyboard"]
    assert len(rows) == 5 and rows[0][0]["callback_data"].startswith("j:s:1:")


def test_header_only_on_first_message(database, make_job, monkeypatch):
    populate(database, make_job, count=8)
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    run_alerts({}, None)
    texts = [call.args[1] for call in send.call_args_list]
    assert len(texts) == 2
    assert "Hôm nay có 8 tin mới" in texts[0] and "Hôm nay" not in texts[1]


def test_hidden_excluded_and_level_filtered_jobs_are_not_alerted(database, make_job, monkeypatch):
    from src.bot.store import UserStore

    jobs, users = database
    users.put_item(Item=Subscription(user_id="123", keyword_raw="data engineer").to_dynamo_item())
    hidden = make_job(job_id="1", company="Hidden Co")
    for job in (hidden, make_job(job_id="2", company="Bán Hàng Đa Cấp"),
                make_job(job_id="3", title="Senior Data Engineer", company="Senior Co"),
                make_job(job_id="4", company="Good Co")):
        jobs.put_item(Item=job)
    store = UserStore()
    store.hide_job("123", identity(hidden))
    store.add_exclude("123", "đa cấp")
    store.set_pref("123", filter_level="junior")
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    assert run_alerts({}, None)["total_matches"] == 1
    assert "Good Co" in send.call_args.args[1]


def test_followed_company_alerts_without_subscription(database, make_job, monkeypatch):
    from src.bot.store import UserStore

    jobs, _ = database
    jobs.put_item(Item=make_job(job_id="1", title="Kế toán", company="Công ty FPT Software"))
    jobs.put_item(Item=make_job(job_id="2", title="Kế toán", company="Other"))
    UserStore().follow("123", "FPT Software")
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr("src.matcher.alerts.TelegramBot.send_message", send)
    summary = run_alerts({}, None)
    assert summary["users_with_follows"] == 1 and summary["notifications_sent"] == 1
    text = send.call_args.args[1]
    assert "Việc mới từ FPT Software" in text and "Other" not in text


def test_mark_sent_keeps_card_payload_for_history(database, make_job):
    store = DeliveryStore(get_settings())
    job = make_job(title="Kế toán", source_url="https://x.vn/1")
    store.mark_sent("123", [job])
    row = database[1].get_item(Key={"user_id": "123", "sk": "SENT#" + identity(job)})["Item"]
    assert row["title"] == "Kế toán" and row["source_url"] == "https://x.vn/1"
