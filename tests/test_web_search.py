from unittest.mock import Mock

import pytest

from src.config import Settings
from src.web_search.you_search import (
    VIETNAM_JOB_DOMAINS,
    YouSearchClient,
    YouSearchError,
)


def make_response(status_code=200, body=None):
    response = Mock(status_code=status_code)
    response.json.return_value = body or {}
    return response


def test_you_search_returns_only_relevant_job_urls(monkeypatch):
    response = make_response(
        body={
            "results": {
                "web": [
                    {
                        "title": "Data Analyst Intern | Acme Vietnam",
                        "url": "https://acme.example/careers/data-analyst?utm_source=test",
                        "description": "Hiring a Data Analyst Internship in Ho Chi Minh.",
                    },
                    {
                        "title": "Business Intelligence course",
                        "url": "https://school.example/course/bi",
                        "description": "Training course and certificate curriculum.",
                    },
                    {
                        "title": "Data Analyst Intern | Acme Vietnam",
                        "url": "https://acme.example/careers/data-analyst?utm_source=duplicate",
                        "description": "Apply for this intern position in Ho Chi Minh.",
                    },
                    {
                        "title": "Find IT Jobs (1043)",
                        "url": "https://careerviet.vn/jobs/data-page.html",
                        "description": "Search results include data analyst intern jobs in HCM.",
                    },
                ],
                "news": [],
            }
        }
    )
    post = Mock(return_value=response)
    monkeypatch.setattr("src.web_search.you_search.requests.post", post)
    settings = Settings(
        you_api_key="secret-test-key",
        you_search_enabled=True,
        you_search_count=20,
    )

    jobs = YouSearchClient(settings).search(
        "data analyst intern", location="HCM", limit=10
    )

    assert len(jobs) == 1
    assert jobs[0]["company"] == "Acme Vietnam"
    assert jobs[0]["location"] == "Ho Chi Minh"
    assert jobs[0]["source_url"] == "https://acme.example/careers/data-analyst"
    assert jobs[0]["is_web_result"] is True
    request = post.call_args
    assert request.kwargs["headers"] == {"X-API-Key": "secret-test-key"}
    assert request.kwargs["json"]["country"] == "VN"
    assert "data analyst" in request.kwargs["json"]["query"]


def test_you_search_maps_credit_error_without_response_body(monkeypatch):
    monkeypatch.setattr(
        "src.web_search.you_search.requests.post",
        Mock(return_value=make_response(status_code=402)),
    )
    client = YouSearchClient(Settings(you_api_key="secret-test-key"))

    with pytest.raises(YouSearchError, match="hết credit"):
        client.search("oracle intern")


def test_major_vietnam_job_boards_are_prioritized():
    assert set(VIETNAM_JOB_DOMAINS) == {
        "topcv.vn",
        "vietnamworks.com",
        "careerviet.vn",
        "jobsgo.vn",
        "vieclam24h.vn",
        "topdev.vn",
    }
