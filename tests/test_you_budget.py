import pytest

from src.config import get_settings
from src.web_search import you_search
from src.web_search.you_search import YouSearchClient


@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.setattr(you_search, "_budget_logged", False)
    get_settings.cache_clear()
    return YouSearchClient(get_settings())


def test_daily_limit_blocks_after_cap(client, database, monkeypatch, caplog):
    _, users = database
    monkeypatch.setenv("VIECLAMBOT_YOU_DAILY_LIMIT", "2")
    calls = []
    monkeypatch.setattr(
        YouSearchClient, "_request", lambda self, *a, **kw: calls.append(1) or []
    )
    for _ in range(4):
        assert client.search("python") == []
    assert len(calls) == 2
    budget = [i for i in users.scan()["Items"] if i["user_id"] == "BUDGET"]
    assert len(budget) == 1 and budget[0]["sk"].startswith("YOU#")
    assert int(budget[0]["count"]) == 4 and "ttl" in budget[0]
    assert caplog.text.count("daily limit") <= 1


def test_default_limit_is_200(monkeypatch):
    monkeypatch.delenv("VIECLAMBOT_YOU_DAILY_LIMIT", raising=False)
    assert you_search._daily_limit() == 200


def test_budget_fails_open_without_dynamodb(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("no dynamodb")

    monkeypatch.setenv("VIECLAMBOT_YOU_DAILY_LIMIT", "1")
    monkeypatch.setattr(you_search.boto3, "resource", boom)
    assert you_search._budget_allows(get_settings()) is True
