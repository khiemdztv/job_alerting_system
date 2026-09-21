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


def test_timviec365_uses_site_search_route(monkeypatch):
    from src.scrapers.timviec365_scraper import TimViec365Scraper

    scraper = TimViec365Scraper()
    get = Mock(return_value=Mock(text="<html></html>"))
    monkeypatch.setattr(scraper, "_get", get)
    scraper.scrape("data engineer", max_pages=1)
    get.assert_called_once_with(
        "https://timviec365.vn/tim-kiem", params={"keyword": "data engineer", "page": 1}
    )


def test_timviec365_chooses_title_instead_of_date_link(monkeypatch):
    from src.scrapers.timviec365_scraper import TimViec365Scraper

    scraper = TimViec365Scraper()
    html = (
        '<div><a href="/data-engineer-p123.html">3 ngày</a>'
        '<h2><a href="/data-engineer-p123.html">Data Engineer</a></h2></div>'
    )
    monkeypatch.setattr(scraper, "_get", Mock(return_value=Mock(text=html)))
    jobs = scraper.scrape("data engineer", max_pages=1)
    assert len(jobs) == 1
    assert jobs[0].title == "Data Engineer"


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
    table.batch_writer.return_value.__enter__ = Mock(return_value=Mock())
    table.batch_writer.return_value.__exit__ = Mock(
        side_effect=ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "BatchWriteItem"
        )
    )
    loader._table = table
    with pytest.raises(ClientError):
        loader.load_batch([Job(title="Accountant", source=JobSource.CAREERLINK)])


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
