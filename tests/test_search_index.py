"""Prebuilt search index: build -> S3 -> load -> candidates -> rank_jobs."""

from __future__ import annotations

import gzip
import pickle
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import boto3
import pytest

from src.config import get_settings
from src.etl.loader import DynamoDBLoader
from src.matcher.search import rank_jobs
from src.search_index import SearchIndex, reset_cache
from src.search_index import builder as builder_module
from src.search_index import reader as reader_module
from src.search_index.builder import build_and_upload, build_from_items
from src.search_index.common import index_key

JOBS = [
    {"job_id": "acc-1", "title": "Kế toán tổng hợp", "company": "Công ty Xây dựng ABC",
     "location": "Hồ Chí Minh", "tags": ["excel"], "source": "careerlink"},
    {"job_id": "acc-2", "title": "General Accountant", "company": "Beta",
     "location": "Hà Nội", "source": "topdev"},
    {"job_id": "audit-1", "title": "Kiểm toán viên", "company": "Gamma",
     "location": "Hồ Chí Minh", "source": "vietnamworks"},
    {"job_id": "elec-1", "title": "Kỹ sư Điện", "company": "Delta",
     "location": "Bình Dương", "source": "careerviet"},
    {"job_id": "rep-1", "title": "Nhân viên đại diện bán hàng", "company": "Epsilon",
     "location": "Đà Nẵng", "source": "jobsgo"},
    {"job_id": "it-1", "title": "IT Helpdesk", "company": "Zeta",
     "location": "Hồ Chí Minh", "source": "itviec"},
    {"job_id": "cpp-1", "title": "Embedded Engineer", "company": "Eta",
     "location": "Hà Nội", "source": "itviec",
     "search_blob": "embedded engineer yêu cầu c++ và linux"},
    {"job_id": "desc-1", "title": "Trợ lý giám đốc", "company": "Theta",
     "location": "Hồ Chí Minh", "source": "careerlink",
     "search_blob": "trợ lý giám đốc, hỗ trợ kế toán nội bộ"},
    {"job_id": "net-1", "title": "Senior .NET Developer", "company": "Iota",
     "location": "Hồ Chí Minh", "source": "topdev", "tags": [".net", "c#"]},
]


def _items(now=None):
    now = now or datetime.now(timezone.utc)
    items = []
    for offset, job in enumerate(JOBS):
        item = {"scraped_at": (now - timedelta(minutes=offset)).isoformat(),
                "source_url": f"https://example.com/{job['job_id']}", **job}
        items.append(item)
    return items


@pytest.fixture
def index_env(database, monkeypatch, tmp_path):
    """Moto DynamoDB + S3 with the jobs loaded and an empty data-lake bucket."""
    monkeypatch.setenv("VIECLAMBOT_SEARCH_INDEX_DIR", str(tmp_path))
    reset_cache()
    settings = get_settings()
    s3 = boto3.client("s3", region_name=settings.aws_region)
    s3.create_bucket(
        Bucket=settings.s3_data_lake_bucket,
        CreateBucketConfiguration={"LocationConstraint": settings.aws_region},
    )
    jobs_table, _ = database
    for item in _items():
        jobs_table.put_item(Item=item)
    yield {"s3": s3, "bucket": settings.s3_data_lake_bucket, "table": jobs_table}
    reset_cache()


def _ids(jobs):
    return {job["job_id"] for job in jobs}


@pytest.mark.parametrize(
    "query,expected",
    [
        ("kế toán", {"acc-1", "acc-2", "desc-1"}),
        ("ke toan", {"acc-1", "acc-2", "desc-1"}),
        ("KẾ TOÁN", {"acc-1", "acc-2", "desc-1"}),
        ("accountant", {"acc-1", "acc-2", "desc-1"}),  # synonym group
        ("kỹ sư điện", {"elec-1"}),
        ("ky su dien", {"elec-1"}),  # "đ" folds to "d" (unicode61 alone keeps it)
        ("kế toán, kiểm toán", {"acc-1", "acc-2", "audit-1", "desc-1"}),
        ("c++", {"cpp-1"}),
        ("senior .net", {"net-1"}),
    ],
)
def test_build_upload_load_candidates(index_env, query, expected):
    summary = build_and_upload(force=True)
    assert summary["status"] == "success" and summary["job_count"] == len(JOBS)

    index = SearchIndex.load(index_env["bucket"])

    assert index is not None and index.job_count == len(JOBS)
    found = _ids(index.candidates([query]))
    assert expected <= found
    # The candidate set never loses a job the full ranking would return.
    everything = _items()
    assert _ids(rank_jobs(everything, query, limit=None, min_score=1)) <= found


def test_upload_metadata_and_sqlite_format(index_env):
    build_and_upload(force=True)
    head = index_env["s3"].head_object(Bucket=index_env["bucket"], Key="index/jobs.sqlite.gz")
    assert head["Metadata"]["job_count"] == str(len(JOBS))
    assert head["Metadata"]["format"] == "sqlite"
    assert datetime.fromisoformat(head["Metadata"]["built_at"])


def test_location_and_company_are_not_matched(index_env):
    build_and_upload(force=True)
    index = SearchIndex.load(index_env["bucket"])
    # "xây dựng" only appears in the company name of acc-1.
    assert "acc-1" not in _ids(index.candidates(["xây dựng"]))
    assert _ids(index.candidates(["kế toán"], "Hà Nội")) == {"acc-2"}


def test_index_path_matches_scan_ranking(index_env, monkeypatch):
    build_and_upload(force=True)
    loader = DynamoDBLoader()
    queries = ["kế toán", "ke toan tong hop", "kế toán, kiểm toán", "IT", "c++", "điện"]
    via_index = {q: loader.search_jobs(q, limit=None, min_score=1) for q in queries}
    monkeypatch.setenv("VIECLAMBOT_SEARCH_INDEX_ENABLED", "false")
    for query in queries:
        scan = loader.search_jobs(query, limit=None, min_score=1)
        assert [j["job_id"] for j in via_index[query]] == [j["job_id"] for j in scan], query


def test_search_jobs_uses_index_not_scan(index_env, monkeypatch):
    build_and_upload(force=True)
    loader = DynamoDBLoader()
    monkeypatch.setattr(loader, "get_search_items", Mock(side_effect=AssertionError("scan")))

    results = loader.search_jobs("ke toan tong hop")

    assert [job["job_id"] for job in results] == ["acc-1"]


def test_etag_cache_downloads_only_when_changed(index_env, monkeypatch):
    build_and_upload(force=True)
    s3 = Mock(wraps=index_env["s3"])

    first = SearchIndex.load(index_env["bucket"], s3_client=s3)
    second = SearchIndex.load(index_env["bucket"], s3_client=s3)
    assert first is second
    assert s3.get_object.call_count == 1
    heads = s3.head_object.call_count  # within the 60 s window: no S3 call at all
    assert SearchIndex.load(index_env["bucket"], s3_client=s3) is first
    assert s3.head_object.call_count == heads

    monkeypatch.setenv("VIECLAMBOT_SEARCH_INDEX_CHECK_SECONDS", "0")
    assert SearchIndex.load(index_env["bucket"], s3_client=s3) is first
    assert s3.get_object.call_count == 1  # HEAD again, same ETag, no download

    index_env["table"].put_item(Item={
        "job_id": "acc-3", "title": "Kế toán kho", "company": "Kappa",
        "location": "Hà Nội", "source": "topcv",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    })
    build_and_upload(force=True)
    refreshed = SearchIndex.load(index_env["bucket"], s3_client=s3)
    assert refreshed is not first and refreshed.etag != first.etag
    assert s3.get_object.call_count == 2
    assert "acc-3" in _ids(refreshed.candidates(["kế toán"]))


def test_fallback_to_scan_when_index_missing(index_env, monkeypatch):
    loader = DynamoDBLoader()
    scan = Mock(wraps=loader.get_search_items)
    monkeypatch.setattr(loader, "get_search_items", scan)

    assert SearchIndex.load(index_env["bucket"]) is None
    results = loader.search_jobs("kế toán tổng hợp")

    assert [job["job_id"] for job in results] == ["acc-1"]
    assert scan.call_count == 1


def test_fallback_to_scan_when_index_is_stale(index_env, monkeypatch):
    build_and_upload(force=True)
    monkeypatch.setenv("VIECLAMBOT_SEARCH_INDEX_MAX_AGE_SECONDS", "-1")
    loader = DynamoDBLoader()
    scan = Mock(wraps=loader.get_search_items)
    monkeypatch.setattr(loader, "get_search_items", scan)

    assert [job["job_id"] for job in loader.search_jobs("kế toán tổng hợp")] == ["acc-1"]
    assert scan.call_count == 1


def test_fallback_to_scan_on_index_error(index_env, monkeypatch):
    loader = DynamoDBLoader()
    monkeypatch.setattr(loader, "_search_index", Mock(side_effect=RuntimeError("corrupt")))
    assert [job["job_id"] for job in loader.search_jobs("kế toán tổng hợp")] == ["acc-1"]


def test_pickle_fallback_when_fts5_is_missing(index_env, monkeypatch):
    monkeypatch.setattr(builder_module, "fts5_available", lambda: False)
    monkeypatch.setattr(reader_module, "fts5_available", lambda: False)

    summary = build_and_upload(force=True)
    index = SearchIndex.load(index_env["bucket"])

    assert summary["format"] == "pickle"
    assert index_env["s3"].head_object(Bucket=index_env["bucket"], Key="index/jobs.pickle.gz")
    assert index.format == "pickle"
    assert {"acc-1", "acc-2"} <= _ids(index.candidates(["ke toan"]))
    assert _ids(index.candidates(["ky su dien"])) == {"elec-1"}
    loader = DynamoDBLoader()
    monkeypatch.setattr(loader, "get_search_items", Mock(side_effect=AssertionError("scan")))
    assert [job["job_id"] for job in loader.search_jobs("kỹ sư điện")] == ["elec-1"]


def test_reader_skips_sqlite_without_fts5_and_uses_newest(index_env, monkeypatch):
    build_and_upload(force=True, fmt="sqlite")
    build_and_upload(force=True, fmt="pickle")
    assert SearchIndex.load(index_env["bucket"]).format == "pickle"  # newer one wins

    reset_cache()
    build_and_upload(force=True, fmt="sqlite")
    assert SearchIndex.load(index_env["bucket"]).format == "sqlite"

    reset_cache()
    monkeypatch.setattr(reader_module, "fts5_available", lambda: False)
    assert SearchIndex.load(index_env["bucket"]).format == "pickle"


def test_matcher_reads_delta_after_index_build(index_env, monkeypatch):
    build_and_upload(force=True)
    index_env["table"].put_item(Item={
        "job_id": "acc-new", "title": "Kế toán thuế", "company": "Lambda",
        "location": "Hà Nội", "source": "topcv",
        "scraped_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
    })
    monkeypatch.setenv("VIECLAMBOT_SEARCH_INDEX_DELTA_AFTER_SECONDS", "-1")
    loader = DynamoDBLoader()
    monkeypatch.setattr(loader, "get_search_items", Mock(side_effect=AssertionError("scan")))
    since = datetime.now(timezone.utc) - timedelta(days=7)

    alerts = loader.search_jobs("kế toán", limit=None, since=since)
    interactive = loader.search_jobs("kế toán", limit=None, min_score=1)

    assert "acc-new" in _ids(alerts)
    assert {"acc-1", "acc-2"} <= _ids(alerts)
    # Interactive search accepts the index lag (documented trade-off).
    assert "acc-new" not in _ids(interactive)


def test_expired_jobs_are_not_indexed(tmp_path):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    items = _items() + [{"job_id": "old", "title": "Kế toán cũ", "expires_at": past,
                         "scraped_at": past, "source": "x"}]
    result = build_from_items(items, tmp_path / "jobs.sqlite", fmt="sqlite")
    index = SearchIndex.open_file(result.path)
    assert result.job_count == len(JOBS)
    assert "old" not in _ids(index.candidates(["kế toán"], limit=None))
    index.close()


def test_candidate_limit_keeps_title_hits_first(tmp_path):
    now = datetime.now(timezone.utc)
    items = [{"job_id": f"d{i}", "title": "Trợ lý", "search_blob": "trợ lý kế toán",
              "scraped_at": (now - timedelta(minutes=i)).isoformat(), "source": "x"}
             for i in range(30)]
    items.append({"job_id": "title", "title": "Kế toán", "source": "x",
                  "scraped_at": (now - timedelta(days=5)).isoformat()})
    index = SearchIndex.open_file(build_from_items(items, tmp_path / "j.sqlite").path)
    assert [job["job_id"] for job in index.candidates(["kế toán"], limit=1)] == ["title"]
    index.close()


def test_pickle_index_refuses_globals(tmp_path):
    path = tmp_path / "evil.pickle"
    path.write_bytes(pickle.dumps(gzip.GzipFile))  # any global reference
    with pytest.raises(pickle.UnpicklingError):
        SearchIndex.open_file(path, "pickle")


def test_indexer_handler_force_and_skip(index_env):
    from lambdas.indexer_handler import handler

    assert handler({"force": True}, None)["status"] == "success"
    assert handler({}, None)["status"] == "skipped"
    assert handler({"force": True}, None)["status"] == "success"
    assert index_key("sqlite") == "index/jobs.sqlite.gz"
