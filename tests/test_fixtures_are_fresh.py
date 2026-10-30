"""Canary: scraper fixtures must be re-captured before they drift from the live sites."""

import json
from datetime import date
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
MANIFEST = FIXTURES / "MANIFEST.json"
MAX_AGE_DAYS = 120


def _load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["fixtures"]


def test_every_fixture_file_is_listed_in_manifest():
    on_disk = {
        p.relative_to(FIXTURES).as_posix()
        for p in FIXTURES.rglob("*")
        if p.is_file() and p.name != "MANIFEST.json"
    }
    listed = set(_load())
    assert on_disk - listed == set(), "add new fixtures to tests/fixtures/MANIFEST.json"
    assert listed - on_disk == set(), "manifest lists fixtures that no longer exist"


def test_fixtures_are_not_older_than_120_days():
    today = date.today()
    stale = {
        name: info["captured_on"]
        for name, info in _load().items()
        if (today - date.fromisoformat(info["captured_on"])).days > MAX_AGE_DAYS
    }
    directories = sorted({name.split("/")[0] for name in stale})
    assert not stale, (
        f"Fixtures older than {MAX_AGE_DAYS} days in {directories}: re-capture them with "
        "scripts/refresh_fixtures.py and update captured_on in tests/fixtures/MANIFEST.json"
    )
