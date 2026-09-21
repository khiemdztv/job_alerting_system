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
