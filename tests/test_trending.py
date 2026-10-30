import json
import time
from datetime import datetime, timedelta, timezone

from src.analytics import trending
from src.analytics.trending import format_trending, normalize_title, trending_summary


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _put(jobs, n, **fields):
    item = {
        "job_id": f"j{n}", "title": "Senior Python Developer", "company": "ACME",
        "location_normalized": "ho chi minh", "source": "topdev", "tags": ["python", "sql"],
        "posted_at": _iso(1), "first_seen_at": _iso(1), "salary_min": 20_000_000,
        "salary_max": 30_000_000,
    }
    item.update(fields)
    item = {k: v for k, v in item.items() if v is not None}
    jobs.put_item(Item=item)


def test_normalize_title():
    assert normalize_title("Senior Python Developer (HCM)") == "python developer"
    assert normalize_title("Fresher Kế toán") == "kế toán"


def test_summary_respects_window_and_computes_medians(database):
    jobs, _ = database
    _put(jobs, 1)
    _put(jobs, 2, title="Junior Python Developer", salary_min=10_000_000, salary_max=None,
         location_normalized="ha noi", source="itviec", company="Beta")
    _put(jobs, 3, posted_at=_iso(30), first_seen_at=_iso(30))  # outside window
    _put(jobs, 4, posted_at=None, first_seen_at=_iso(2), title="Accountant", tags=["excel"],
         salary_min=None, salary_max=None)
    summary = trending_summary(days=7)
    assert summary["total_jobs"] == 3
    assert summary["top_skills"][0] == ["python", 2]
    assert summary["top_titles"][0] == ["python developer", 2]
    assert summary["median_salary"] == int((25_000_000 + 10_000_000) / 2)
    assert dict(summary["sources"]) == {"topdev": 2, "itviec": 1}
    cities = {c["city"]: c for c in summary["top_cities"]}
    assert cities["ha noi"]["median_salary"] == 10_000_000
    assert dict(summary["top_companies"])["ACME"] == 2


def test_city_filter_and_cache(database, monkeypatch):
    jobs, users = database
    _put(jobs, 1)
    _put(jobs, 2, location_normalized="ha noi")
    first = trending_summary(days=7, city="Hà Nội")
    assert first["total_jobs"] == 1
    cached = users.get_item(Key={"user_id": "CACHE", "sk": "TRENDING#7d#ha noi"})["Item"]
    assert int(cached["ttl"]) > time.time() + 5 * 3600
    assert json.loads(cached["payload"])["total_jobs"] == 1

    def no_scan(*args):
        raise AssertionError("cache should prevent a rescan")

    monkeypatch.setattr(trending, "_scan_window", no_scan)
    assert trending_summary(days=7, city="Hà Nội") == first


def test_format_escapes_html_and_stays_short(database):
    jobs, _ = database
    for n in range(40):
        _put(jobs, n, title=f"Role {n} x<y", company=f"Company {n} & Co", tags=[f"skill{n}"],
             source=f"src{n}")
    text = format_trending(trending_summary(days=7))
    assert len(text) <= 3500
    assert "&amp;" in text and "<b>Xu hướng" in text and "x<y" not in text


def test_format_truncates_huge_summary():
    summary = {"days": 7, "total_jobs": 1, "top_skills": [["x" * 200, 1]] * 15,
               "top_titles": [["y" * 200, 1]] * 10, "top_companies": [["z" * 200, 1]] * 10,
               "top_cities": [], "sources": [["s" * 100, 1]] * 20}
    assert len(format_trending(summary)) <= 3500
