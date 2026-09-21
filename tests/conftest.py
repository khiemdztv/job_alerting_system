import os

import boto3
import pytest
from moto import mock_aws

os.environ.update(
    {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "ap-southeast-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "VIECLAMBOT_TELEGRAM_BOT_TOKEN": "test-token",
        "VIECLAMBOT_JOOBLE_API_KEY": "",
        "VIECLAMBOT_ADMIN_CHAT_ID": "",
    }
)


@pytest.fixture(autouse=True)
def no_real_telegram(monkeypatch):
    import requests

    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not send network requests")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture
def database():
    from src.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        jobs = dynamodb.create_table(
            TableName=settings.dynamodb_jobs_table,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        )
        users = dynamodb.create_table(
            TableName=settings.dynamodb_users_table,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
        )
        yield jobs, users


@pytest.fixture
def make_job():
    from datetime import datetime, timezone

    def make(title="Data Engineer", **changes):
        job = {
            "job_id": "job-1",
            "title": title,
            "company": "Example",
            "location": "Hồ Chí Minh",
            "source": "careerlink",
            "source_url": "https://example.com/jobs/1",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        job.update(changes)
        return job

    return make
