import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from lambdas.etl_handler import handler as etl_handler
from lambdas.scraper_handler import _push_to_sqs, _scrape_source
from src.common.models import Job, JobSource, RawJob, Subscription
from src.config import get_settings
from src.etl.loader import DynamoDBLoader
from src.etl.transformer import Transformer
from src.matcher.keyword_matcher import KeywordMatcher


@pytest.mark.parametrize("salary", ["15 - 30 triệu, đào tạo tại Hongkong...", "..."])
def test_salary_punctuation_does_not_drop_job(salary):
    job = Transformer().transform(
        RawJob(title="Sales Staff", source=JobSource.YBOX, salary_raw=salary)
    )
    assert job is not None
    expected = (15_000_000, 30_000_000) if salary.startswith("15") else (None, None)
    assert (job.salary_min, job.salary_max) == expected


def test_source_url_identity_does_not_merge_different_posts():
    first = Job(title="Accountant", source=JobSource.CAREERLINK, source_url="https://example.com/1")
    second = Job(
        title="Accountant", source=JobSource.CAREERLINK, source_url="https://example.com/2"
    )
    tracked = Job(
        title="Updated title",
        source=JobSource.CAREERLINK,
        source_url="https://example.com/1?utm_source=telegram#top",
    )
    assert first.computed_job_id != second.computed_job_id
    assert first.computed_job_id == tracked.computed_job_id


def test_timviec365_uses_category_listing_not_disallowed_search(monkeypatch):
    # /tim-kiem is disallowed by robots.txt and takes ~7.5 s; keywords map to the
    # closest category listing instead ("data engineer" -> IT phần mềm, c13).
    from src.scrapers.timviec365_scraper import TimViec365Scraper

    scraper = TimViec365Scraper()
    get = Mock(return_value=Mock(text="<html></html>"))
    monkeypatch.setattr(scraper, "_get", get)
    scraper.scrape("data engineer", max_pages=1)
    get.assert_called_once_with("https://timviec365.vn/viec-lam-it-phan-mem-c13v0", params=None)


def test_timviec365_uses_card_age_not_deadline_as_posted_date(monkeypatch):
    from src.scrapers.timviec365_scraper import TimViec365Scraper

    scraper = TimViec365Scraper()
    html = (
        '<div class="item_vl"><a class="logo_user_th" href="/data-engineer-p123.html">'
        '<span class="box_time_off">3 ngày</span></a>'
        '<h2><a class="title_new" href="/data-engineer-p123.html" title="Data Engineer">'
        "Data Engineer</a></h2>"
        '<span class="job_time">30/11/2026</span></div>'
    )
    monkeypatch.setattr(scraper, "_get", Mock(return_value=Mock(text=html)))
    jobs = scraper.scrape("data engineer", max_pages=1)
    assert len(jobs) == 1
    assert jobs[0].title == "Data Engineer"
    assert jobs[0].posted_at_raw == "3 ngày trước"


def test_sqs_retries_only_rejected_entries(monkeypatch):
    sqs = Mock()
    sqs.get_queue_url.return_value = {"QueueUrl": "test-queue"}
    sqs.send_message_batch.side_effect = [{"Failed": [{"Id": "1"}]}, {"Failed": []}]
    monkeypatch.setattr("boto3.client", Mock(return_value=sqs))
    monkeypatch.setattr("lambdas.scraper_handler.time.sleep", Mock())
    jobs = [RawJob(title="Accountant", source=JobSource.CAREERLINK) for _ in range(2)]
    _push_to_sqs(jobs, get_settings())
    assert len(sqs.send_message_batch.call_args_list[0].kwargs["Entries"]) == 2
    assert [e["Id"] for e in sqs.send_message_batch.call_args_list[1].kwargs["Entries"]] == ["1"]


def test_sqs_persistent_failure_propagates(monkeypatch):
    sqs = Mock()
    sqs.get_queue_url.return_value = {"QueueUrl": "test-queue"}
    sqs.send_message_batch.return_value = {"Failed": [{"Id": "0"}]}
    monkeypatch.setattr("boto3.client", Mock(return_value=sqs))
    monkeypatch.setattr("lambdas.scraper_handler.time.sleep", Mock())
    with pytest.raises(RuntimeError):
        _push_to_sqs([RawJob(title="Accountant", source=JobSource.CAREERLINK)], get_settings())


def test_failed_batch_flush_is_not_counted_as_success():
    loader = DynamoDBLoader()
    table = Mock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
    )
    loader._table = table
    with pytest.raises(ClientError):
        loader.load_batch([Job(title="Accountant", source=JobSource.CAREERLINK)])


def test_upsert_keeps_first_seen_at_and_refreshes_other_fields(database):
    loader = DynamoDBLoader()
    job = Job(title="Accountant", company="ACME", source=JobSource.CAREERLINK,
              source_url="https://example.com/jobs/1")
    job.generate_id()
    loader.load_batch([job])
    first = loader.table.get_item(Key={"job_id": job.job_id})["Item"]
    job.company = "ACME Updated"
    loader.load_batch([job])
    second = loader.table.get_item(Key={"job_id": job.job_id})["Item"]
    assert second["company"] == "ACME Updated"
    assert second["first_seen_at"] == first["first_seen_at"]


def test_etl_bad_record_fails_for_retry():
    with pytest.raises(ValueError):
        etl_handler({"Records": [{"body": "not-json"}]}, None)


def test_etl_keeps_source_variants_and_is_idempotent(database, monkeypatch):
    monkeypatch.setattr(
        "lambdas.etl_handler.S3Loader.save_processed_data", Mock(return_value="saved")
    )
    records = [
        {
            "body": json.dumps(
                {
                    "title": "Accountant",
                    "company": "Example",
                    "source": source,
                    "source_url": f"https://{source}.vn/job/1",
                }
            )
        }
        for source in ("careerlink", "jooble")
    ]
    for _ in range(2):
        assert etl_handler({"Records": records}, None)["loaded_to_db"] == 2
    assert len(database[0].scan()["Items"]) == 2


def test_paginated_subscriptions_preserve_filters(monkeypatch):
    table = Mock()
    first = Subscription(user_id="1", keyword_raw="python").to_dynamo_item()
    second = Subscription(
        user_id="2", keyword_raw="python", location_filter="HCM", salary_min_filter=15000000
    ).to_dynamo_item()
    table.scan.side_effect = [
        {"Items": [first], "LastEvaluatedKey": {"user_id": "1"}},
        {"Items": [second]},
    ]
    resource = Mock()
    resource.Table.return_value = table
    monkeypatch.setattr("boto3.resource", Mock(return_value=resource))
    result = KeywordMatcher().get_all_active_subscriptions("test")
    assert result["2"][0].location_filter == "HCM"
    assert result["2"][0].salary_min_filter == 15000000


def test_scraper_honors_page_setting_and_flushes_each_keyword(monkeypatch):
    import time

    scraper = Mock(source=JobSource.CAREERLINK, last_error="")
    scraper.scrape_safe.return_value = [
        RawJob(
            title="Accountant",
            source=JobSource.CAREERLINK,
            company="Example",
            source_url="https://example.com/job",
        )
    ]
    pushed = Mock()
    monkeypatch.setattr("lambdas.scraper_handler._push_to_sqs", pushed)
    monkeypatch.setattr("lambdas.scraper_handler._save_raw_to_s3", Mock())
    monkeypatch.setattr("lambdas.scraper_handler.record_health", Mock())
    settings = get_settings()
    result = _scrape_source(scraper, ["accountant", "python"], settings, time.monotonic() + 120)
    assert result["keywords_completed"] == 2
    assert pushed.call_count == 2
    assert scraper.scrape_safe.call_args.kwargs["max_pages"] == settings.scrape_max_pages


def test_blocked_source_is_reported_as_blocked_not_error(monkeypatch):
    import requests

    from src.common.models import JobSource
    from src.scrapers.careerlink_scraper import CareerLinkScraper

    scraper = CareerLinkScraper()
    response = Mock(status_code=403)
    error = requests.HTTPError("403 Client Error", response=response)
    monkeypatch.setattr(scraper.session, "get", Mock(side_effect=error))
    with pytest.raises(requests.HTTPError):
        scraper._get("https://www.careerlink.vn/vieclam/list")
    assert scraper.last_error == "HTTP403"

    recorded = {}
    monkeypatch.setattr(
        "lambdas.scraper_handler.record_health", lambda result, settings: recorded.update(
            {"status": result.status, "error": result.error}
        )
    )
    monkeypatch.setattr(scraper, "scrape_safe", Mock(side_effect=lambda *a, **k: (
        setattr(scraper, "last_error", "HTTP403"), [])[1]))
    import time

    summary = _scrape_source(scraper, ["kế toán"], get_settings(), time.monotonic() + 60)
    assert summary["status"] == "blocked"
    assert recorded["status"].value == "blocked"
    assert scraper.source is JobSource.CAREERLINK


def test_scheduled_run_reads_newest_listings_before_keywords(monkeypatch):
    import time

    from src.common.models import JobSource, RawJob
    from src.scrapers.base_scraper import BaseScraper

    class LatestBoard(BaseScraper):
        supports_latest = True

        def __init__(self):
            super().__init__(source=JobSource.CAREERVIET)
            self.calls = []

        def scrape(self, keyword, max_pages=None):
            self.calls.append(("keyword", keyword))
            return []

        def scrape_latest(self, max_pages=None):
            self.calls.append(("latest", max_pages))
            return [RawJob(title="Kế toán", company="ACME", source=self.source,
                           source_url="https://careerviet.vn/jobs/1")]

        def parse_job(self, raw_data):
            return None

    pushed = Mock()
    monkeypatch.setattr("lambdas.scraper_handler._push_to_sqs", pushed)
    monkeypatch.setattr("lambdas.scraper_handler._save_raw_to_s3", Mock())
    monkeypatch.setattr("lambdas.scraper_handler.record_health", Mock())
    scraper = LatestBoard()
    settings = get_settings()

    summary = _scrape_source(scraper, ["python"], settings, time.monotonic() + 60)

    assert scraper.calls[0] == ("latest", settings.scrape_latest_pages)
    assert scraper.calls[1] == ("keyword", "python")
    assert summary["valid"] == 1 and summary["status"] == "ok"
    assert pushed.call_count == 1


def test_layout_change_is_reported_distinctly(monkeypatch):
    import time

    from src.scrapers.base_scraper import LAYOUT_CHANGED
    from src.scrapers.careerlink_scraper import CareerLinkScraper

    scraper = CareerLinkScraper()
    monkeypatch.setattr("lambdas.scraper_handler.record_health", Mock())
    monkeypatch.setattr(scraper, "scrape_latest", Mock(return_value=[]))
    monkeypatch.setattr(scraper, "scrape_safe", Mock(side_effect=lambda *a, **k: (
        scraper.mark_layout_changed(), [])[1]))

    summary = _scrape_source(scraper, ["kế toán"], get_settings(), time.monotonic() + 60)

    assert scraper.last_error == LAYOUT_CHANGED
    assert summary["status"] == "layout_changed"


def test_scrape_safe_forwards_location_only_when_supported():
    from src.common.models import JobSource
    from src.scrapers.base_scraper import BaseScraper

    class WithLocation(BaseScraper):
        def __init__(self):
            super().__init__(source=JobSource.CAREERVIET)
            self.seen = None

        def scrape(self, keyword, max_pages=None, location=None):
            self.seen = location
            return []

        def parse_job(self, raw_data):
            return None

    class WithoutLocation(BaseScraper):
        def __init__(self):
            super().__init__(source=JobSource.CAREERVIET)

        def scrape(self, keyword, max_pages=None):
            return []

        def parse_job(self, raw_data):
            return None

    first = WithLocation()
    first.scrape_safe("kế toán", 1, location="Hà Nội")
    assert first.supports_location and first.seen == "Hà Nội"
    second = WithoutLocation()
    assert not second.supports_location
    assert second.scrape_safe("kế toán", 1, location="Hà Nội") == []


def test_board_job_type_label_beats_description_wording():
    from src.common.models import JobSource, JobType, RawJob
    from src.etl.transformer import Transformer

    raw = RawJob(title="Marketing Intern", company="ACME", source=JobSource.YBOX,
                 job_type_raw="internship", description="Full time, 3 months",
                 source_url="https://ybox.vn/1")
    assert Transformer().transform(raw).job_type is JobType.INTERNSHIP


def test_jsonld_fallback_runs_when_dedicated_parser_finds_nothing(monkeypatch):
    import time

    from src.common.models import JobSource, RawJob
    from src.scrapers.careerviet_scraper import CareerVietScraper
    from src.scrapers.generic_jsonld_scraper import KNOWN_SITES, GenericJsonLdScraper

    scraper = CareerVietScraper()
    monkeypatch.setattr(scraper, "scrape_safe", Mock(side_effect=lambda *a, **k: (
        scraper.mark_layout_changed(), [])[1]))
    site = next(site for site in KNOWN_SITES if site.source is JobSource.CAREERVIET)
    fallback = GenericJsonLdScraper(site)
    job = RawJob(title="Kế toán", company="ACME", source=JobSource.CAREERVIET,
                 source_url="https://careerviet.vn/vi/tim-viec-lam/ke-toan.35A1.html")
    monkeypatch.setattr(fallback, "scrape_latest", Mock(return_value=[job]))
    monkeypatch.setattr(fallback, "scrape_safe", Mock(return_value=[]))
    scraper.fallback = fallback
    pushed = Mock()
    monkeypatch.setattr("lambdas.scraper_handler._push_to_sqs", pushed)
    monkeypatch.setattr("lambdas.scraper_handler._save_raw_to_s3", Mock())
    monkeypatch.setattr("lambdas.scraper_handler.record_health", Mock())

    summary = _scrape_source(scraper, ["kế toán"], get_settings(), time.monotonic() + 300)

    assert summary["fallback_used"] is True
    assert summary["valid"] == 1 and summary["status"] == "ok"
    assert pushed.call_count == 1


def test_supports_latest_is_detected_from_override():
    from src.scrapers.job123_scraper import Job123Scraper
    from src.scrapers.linkedin_scraper import LinkedInScraper
    from src.scrapers.vietnamworks_scraper import VietnamWorksScraper

    assert Job123Scraper().supports_latest is True
    assert LinkedInScraper().supports_latest is False
    # Whatever the dedicated parser does, the flag is a plain bool.
    assert isinstance(VietnamWorksScraper().supports_latest, bool)


def test_unreachable_source_stops_after_two_failed_attempts(monkeypatch):
    import time

    from src.scrapers.careerlink_scraper import CareerLinkScraper

    scraper = CareerLinkScraper()
    calls = []

    def failing(keyword, max_pages=None, **kwargs):
        calls.append(keyword)
        scraper.last_error = "ConnectTimeout"
        return []

    monkeypatch.setattr(scraper, "scrape_latest", Mock(return_value=[]))
    monkeypatch.setattr(scraper, "scrape_safe", failing)
    monkeypatch.setattr("lambdas.scraper_handler.record_health", Mock())

    summary = _scrape_source(
        scraper, ["kế toán", "marketing", "python", "sales"], get_settings(),
        time.monotonic() + 300,
    )

    assert calls == ["kế toán", "marketing"]
    assert summary["status"] == "error"
    assert summary["keywords_deferred"] == 2


def test_etl_stores_enrichment_and_group_id(database, monkeypatch):
    monkeypatch.setattr(
        "lambdas.etl_handler.S3Loader.save_processed_data", Mock(return_value="saved")
    )
    monkeypatch.setattr("lambdas.etl_handler.save_processed_ndjson", Mock(return_value="k"))
    records = [
        {"body": json.dumps({
            "title": title, "company": company, "location": "Hà Nội", "source": source,
            "source_url": f"https://{source}.vn/job/1", "salary_raw": "15 - 20 triệu",
            "description": "Yêu cầu 2-3 năm kinh nghiệm Java, Spring. Lương tháng 13, BHXH.",
        })}
        for title, company, source in (
            ("Senior Java Developer", "Công ty CP Giải Pháp Thiên Hoàng", "jobsgo"),
            ("Java Developer (Urgent)", "GIẢI PHÁP THIÊN HOÀNG JSC", "indeed"),
        )
    ]
    assert etl_handler({"Records": records}, None)["loaded_to_db"] == 2
    items = database[0].scan()["Items"]
    assert len({i["job_id"] for i in items}) == 2
    assert len({i["canonical_group_id"] for i in items}) == 1
    for item in items:
        assert item["industry"] == "it_phan_mem"
        assert item["experience_min_years"] == 2 and item["experience_max_years"] == 3
        assert {"13th_month", "insurance"} <= set(item["benefits"])
        assert "java" in item["skills"] and item["tags"] == item["skills"]
        assert item["salary_period"] == "month" and 0 < item["quality_score"] <= 100


def test_etl_writes_gzip_ndjson_for_analytics(database, monkeypatch):
    import gzip

    import boto3

    settings = get_settings()
    s3 = boto3.client("s3", region_name=settings.aws_region)
    s3.create_bucket(
        Bucket=settings.s3_data_lake_bucket,
        CreateBucketConfiguration={"LocationConstraint": settings.aws_region},
    )
    records = [{"body": json.dumps({"title": "Accountant", "company": "ACME",
                                    "source": "careerlink",
                                    "source_url": "https://careerlink.vn/job/1"})}]
    etl_handler({"Records": records}, None)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=settings.s3_data_lake_bucket)["Contents"]]
    ndjson = [k for k in keys if k.startswith("processed/ndjson/") and k.endswith(".ndjson.gz")]
    assert len(ndjson) == 1 and any(k.endswith(".json") for k in keys)
    body = s3.get_object(Bucket=settings.s3_data_lake_bucket, Key=ndjson[0])["Body"].read()
    rows = [json.loads(line) for line in gzip.decompress(body).decode("utf-8").splitlines()]
    assert rows[0]["title"] == "Accountant" and rows[0]["level"] == "unknown"
    assert "search_blob" not in rows[0] and rows[0]["canonical_group_id"]
