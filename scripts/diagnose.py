"""Read-only operational checks. Does not deploy, modify webhooks, or send messages."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
import requests
from botocore.config import Config

from src.config import get_settings


def telegram_status(settings):
    if not settings.telegram_bot_token:
        return {"status": "missing_token"}
    result = {}
    for method in ("getMe", "getWebhookInfo"):
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}", timeout=15
            )
            body = response.json()
            if not body.get("ok"):
                result[method] = {"status": response.status_code}
                continue
            data = body["result"]
            if method == "getMe":
                result[method] = {"ok": True, "username": data.get("username")}
            else:
                result[method] = {
                    "has_url": bool(data.get("url")),
                    "pending_update_count": data.get("pending_update_count", 0),
                    "last_error_date": data.get("last_error_date"),
                    "last_error_message": str(data.get("last_error_message", "")).replace(
                        settings.telegram_bot_token, "[REDACTED]"
                    ),
                }
        except Exception as exc:
            result[method] = {"error": type(exc).__name__}
    return result


def aws_status(settings):
    config = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})

    def client(service):
        return boto3.client(service, region_name=settings.aws_region, config=config)

    try:
        client("sts").get_caller_identity()
    except Exception as exc:
        return {
            "status": "unavailable",
            "error": type(exc).__name__,
            "code": getattr(exc, "response", {}).get("Error", {}).get("Code", ""),
        }
    result = {}
    events, lambdas, logs = client("events"), client("lambda"), client("logs")
    since = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp() * 1000)
    for name in ("scraper", "etl", "matcher", "webhook"):
        function = f"vieclambot-{name}"
        try:
            conf = lambdas.get_function_configuration(FunctionName=function)
            result[function] = {
                key: conf.get(key)
                for key in (
                    "Runtime",
                    "State",
                    "LastUpdateStatus",
                    "LastModified",
                    "Timeout",
                    "MemorySize",
                )
            }
            streams = logs.describe_log_streams(
                logGroupName=f"/aws/lambda/{function}",
                orderBy="LastEventTime",
                descending=True,
                limit=1,
            )
            result[function]["last_log_event"] = next(iter(streams.get("logStreams", [])), {}).get(
                "lastEventTimestamp"
            )
            errors = logs.filter_log_events(
                logGroupName=f"/aws/lambda/{function}",
                startTime=since,
                filterPattern='?ERROR ?"Task timed out"',
                limit=30,
            )
            messages = [e["message"] for e in errors.get("events", [])]
            result[function]["recent_error_sample_count"] = len(messages)
            result[function]["error_types"] = [
                pattern
                for pattern in (
                    "ImportModuleError",
                    "Task timed out",
                    "AccessDenied",
                    "ValidationException",
                    "message is too long",
                    "parse entities",
                    "Forbidden",
                    "chat not found",
                    "NoCredentials",
                    "NameError",
                    "incomplete",
                )
                if any(pattern in m for m in messages)
            ]
        except Exception as exc:
            result.setdefault(function, {})["error"] = type(exc).__name__
    for name in ("vieclambot-scraper-schedule", "vieclambot-matcher-schedule"):
        try:
            rule = events.describe_rule(Name=name)
            result[name] = {
                "state": rule["State"],
                "schedule": rule.get("ScheduleExpression"),
                "targets": len(events.list_targets_by_rule(Rule=name)["Targets"]),
            }
        except Exception as exc:
            result[name] = {"error": type(exc).__name__}
    try:
        sqs = client("sqs")
        url = sqs.get_queue_url(QueueName=settings.sqs_raw_jobs_queue)["QueueUrl"]
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "VisibilityTimeout",
                "RedrivePolicy",
            ],
        )["Attributes"]
        result["queue"] = attrs
        result["etl_mappings"] = [
            {key: mapping.get(key) for key in ("State", "LastProcessingResult", "BatchSize")}
            for mapping in lambdas.list_event_source_mappings(FunctionName="vieclambot-etl")[
                "EventSourceMappings"
            ]
        ]
        dynamodb = client("dynamodb")
        result["users_ttl"] = dynamodb.describe_time_to_live(
            TableName=settings.dynamodb_users_table
        )["TimeToLiveDescription"]
    except Exception as exc:
        result["storage_error"] = type(exc).__name__
    return result


def probe_sources(keyword: str):
    import time

    from lambdas.scraper_handler import (
        CareerLinkScraper,
        CareerVietScraper,
        ChototScraper,
        ITviecScraper,
        JoobleScraper,
        TimViec365Scraper,
        ViecLam24hScraper,
        YBoxScraper,
    )
    from src.etl.transformer import Transformer
    from src.matcher.search import rank_jobs

    def probe(cls):
        scraper = cls()
        scraper.deadline_at = time.monotonic() + 50
        try:
            raw = scraper.scrape_safe(keyword, max_pages=1)
            jobs = [job.to_dynamo_item() for job in Transformer().transform_batch(raw)]
            return scraper.source.value, jobs, scraper.last_error
        finally:
            scraper.session.close()

    classes = [
        CareerLinkScraper,
        ViecLam24hScraper,
        CareerVietScraper,
        ITviecScraper,
        TimViec365Scraper,
        YBoxScraper,
        ChototScraper,
        JoobleScraper,
    ]
    summary, jobs = {}, []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for source, source_jobs, error in pool.map(probe, classes):
            summary[source] = {
                "raw_jobs": len(source_jobs),
                "relevant_jobs": len(rank_jobs(source_jobs, keyword, limit=None)),
                "error": error,
            }
            jobs.extend(source_jobs)
    output = Path(__file__).resolve().parents[1] / "dist/probe-jobs.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-aws", action="store_true")
    parser.add_argument("--sources", action="store_true")
    parser.add_argument("--keyword", default="data engineer")
    args = parser.parse_args()
    settings = get_settings()
    # Third-party request errors can contain API credentials in URLs.
    logging.disable(logging.CRITICAL)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "telegram": telegram_status(settings),
    }
    if not args.skip_aws:
        report["aws"] = aws_status(settings)
    if args.sources:
        report["sources"] = probe_sources(args.keyword)
    print(json.dumps(report, ensure_ascii=True, indent=2))
