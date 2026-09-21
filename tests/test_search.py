from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from src.etl.loader import DynamoDBLoader
from src.matcher.search import parse_search, rank_jobs, relevance


@pytest.mark.parametrize(
    "query,title",
    [
        ("ke toan", "Kế toán tổng hợp"),
        ("nhân viên kế toán", "Accountant"),
        ("Tìm việc thực tập marketing", "Marketing Internship"),
        ("kỹ sư dữ liệu", "Data Engineer"),
        ("C++", "C++ Developer"),
        ("C#", "C# Developer"),
        ("R", "R Developer"),
    ],
)
def test_equivalent_queries(query, title, make_job):
    assert relevance(make_job(title), query) > 0


@pytest.mark.parametrize(
    "query,title",
    [
        ("it", "Digital Marketing"),
        ("hr", "Chrome Developer"),
        ("data engineer", "Data Analyst"),
        ("điều dưỡng", "Bác sĩ"),
        ("", "Data Engineer"),
        ("---", "Data Engineer"),
        ("C++", "C Developer"),
        ("java", "JavaScript Developer"),
    ],
)
def test_no_substring_or_wrong_occupation(query, title, make_job):
    assert relevance(make_job(title), query) == 0


def test_legacy_description_is_searchable(make_job):
    job = make_job("Backend Developer", description="Build ETL pipelines with Python")
    assert rank_jobs([job], "python") == [job]


def test_location_and_salary_applied_before_limit(make_job):
    jobs = [make_job(company=str(i), location="Hà Nội") for i in range(60)]
    target = make_job(company="Match", salary_max=20000000)
    jobs.append(target)
    assert rank_jobs(jobs, "data engineer", location="HCM", salary_min=15000000, limit=1) == [
        target
    ]


def test_recency_does_not_hide_older_active_jobs(make_job):
    older = make_job(
        company="Older", posted_at=(datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    )
    newer = make_job()
    expired = make_job(company="Expired", ttl=1)
    assert rank_jobs([newer, older, expired], "data engineer") == [newer, older]


def test_ranking_before_source_and_duplicates(make_job):
    exact = make_job(company="Exact")
    weaker = make_job(
        "Backend Developer", company="Other", source="aaa", description="Data engineer skills"
    )
    mirror = dict(exact, source="jooble", job_id="mirror")
    results = rank_jobs([weaker, exact, mirror], "data engineer")
    assert len(results) == 2
    assert results[0]["company"] == "Exact"


def test_scans_beyond_two_pages_and_reuses_snapshot(make_job):
    loader = DynamoDBLoader()
    table = Mock()
    table.scan.side_effect = [
        {"Items": [], "LastEvaluatedKey": {"job_id": "a"}},
        {"Items": [], "LastEvaluatedKey": {"job_id": "b"}},
        {"Items": [make_job()]},
    ]
    loader._table = table
    assert len(loader.search_jobs("data engineer")) == 1
    assert len(loader.search_jobs("kỹ sư dữ liệu")) == 1
    assert table.scan.call_count == 3


def test_storage_failure_is_not_empty_result():
    loader = DynamoDBLoader()
    loader._table = Mock()
    loader._table.scan.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "Scan"
    )
    with pytest.raises(ClientError):
        loader.search_jobs("python")


def test_scan_deadline_returns_partial_snapshot(make_job, monkeypatch):
    loader = DynamoDBLoader()
    table = Mock()
    table.scan.return_value = {
        "Items": [make_job()],
        "LastEvaluatedKey": {"job_id": "next"},
    }
    loader._table = table
    clock = iter([0.0, 2.0, 2.1, 2.2])
    monkeypatch.setattr("src.etl.loader.time.monotonic", lambda: next(clock))

    results = loader.search_jobs("data engineer", deadline_at=1.0)

    assert len(results) == 1
    assert table.scan.call_count == 1
    assert loader._last_search_complete is False
    assert len(loader.search_jobs("data engineer", deadline_at=3.0)) == 1
    assert table.scan.call_count == 1


@pytest.mark.parametrize(
    "query,expected",
    [
        ("kế toán | HCM", ("kế toán", "HCM")),
        ("tìm việc python tại Hà Nội", ("tìm việc python", "Hà Nội")),
        ("data engineer", ("data engineer", None)),
    ],
)
def test_natural_query_location(query, expected):
    assert parse_search(query) == expected
