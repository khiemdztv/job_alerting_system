"""Deploy reviewed code using a named AWS profile, preserving existing secrets and triggers."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]


def deploy(profile: str):
    session = boto3.Session(profile_name=profile, region_name="ap-southeast-1")
    client = session.client("lambda")
    package = (ROOT / "dist/deployment-reviewed.zip").read_bytes()
    expected_hash = base64.b64encode(hashlib.sha256(package).digest()).decode()
    definitions = {
        "etl": (256, 60),
        "webhook": (1024, 30),
        "matcher": (1024, 300),
        "scraper": (512, 900),
        "indexer": (1024, 300),
    }
    # /tmp (MB): the webhook and matcher unpack the search index there.
    ephemeral = {"webhook": 1024, "matcher": 1024, "indexer": 1024}
    configs = {}
    for kind in definitions:
        name = "vieclambot-" + kind
        if kind == "indexer":
            conf = _ensure_indexer(client, package, configs["matcher"])
        else:
            conf = client.get_function_configuration(FunctionName=name)
        if conf["Runtime"] != "python3.12" or conf.get("Architectures") != ["x86_64"]:
            raise RuntimeError(f"Runtime mismatch: {name}")
        configs[kind] = conf
    for kind, (memory, timeout) in definitions.items():
        name = "vieclambot-" + kind
        conf = configs[kind]
        variables = dict(conf.get("Environment", {}).get("Variables", {}))
        variables.update(
            {
                "VIECLAMBOT_ALERT_MAX_JOBS_PER_USER": "15",
                "VIECLAMBOT_ALERT_RECOVERY_DAYS": "7",
                "VIECLAMBOT_SCRAPE_WORKERS": "4",
                "VIECLAMBOT_SCRAPE_MAX_PAGES": "5",
                "VIECLAMBOT_SEARCH_RESULT_LIMIT": "100",
                "VIECLAMBOT_MAX_SUBSCRIPTIONS": "10",
            }
        )
        client.update_function_configuration(
            FunctionName=name,
            MemorySize=memory,
            Timeout=timeout,
            Environment={"Variables": variables},
            EphemeralStorage={"Size": ephemeral.get(kind, 512)},
            RevisionId=conf["RevisionId"],
        )
        client.get_waiter("function_updated_v2").wait(FunctionName=name)
        current = client.get_function_configuration(FunctionName=name)
        client.update_function_code(
            FunctionName=name, ZipFile=package, RevisionId=current["RevisionId"]
        )
        client.get_waiter("function_updated_v2").wait(FunctionName=name)
        after = client.get_function_configuration(FunctionName=name)
        if after["CodeSha256"] != expected_hash or after["LastUpdateStatus"] != "Successful":
            raise RuntimeError(f"Deployment verification failed: {name}")
        print(
            json.dumps(
                {
                    "function": name,
                    "status": after["LastUpdateStatus"],
                    "memory": memory,
                    "timeout": timeout,
                    "code_hash": expected_hash,
                }
            ),
            flush=True,
        )

    dynamodb = session.client("dynamodb")
    users_table = configs["matcher"]["Environment"]["Variables"]["VIECLAMBOT_DYNAMODB_USERS_TABLE"]
    ttl = dynamodb.describe_time_to_live(TableName=users_table)["TimeToLiveDescription"]
    if ttl["TimeToLiveStatus"] == "DISABLED":
        dynamodb.update_time_to_live(
            TableName=users_table, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"}
        )
    elif ttl.get("AttributeName") != "ttl":
        raise RuntimeError("Users table has a different TTL attribute; inspect before changing")

    sqs = session.client("sqs")
    queue_name = configs["scraper"]["Environment"]["Variables"]["VIECLAMBOT_SQS_RAW_JOBS_QUEUE"]
    queue_url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    attrs = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["RedrivePolicy", "VisibilityTimeout", "QueueArn"]
    )["Attributes"]
    updates = {"VisibilityTimeout": str(max(180, int(attrs["VisibilityTimeout"])))}
    if not attrs.get("RedrivePolicy"):
        dlq_url = sqs.create_queue(
            QueueName=queue_name + "-dlq", Attributes={"MessageRetentionPeriod": "1209600"}
        )["QueueUrl"]
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        updates["RedrivePolicy"] = json.dumps(
            {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"}
        )
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes=updates)

    mappings = client.list_event_source_mappings(
        FunctionName="vieclambot-etl", EventSourceArn=attrs["QueueArn"]
    )["EventSourceMappings"]
    for mapping in mappings:
        client.update_event_source_mapping(
            UUID=mapping["UUID"], ScalingConfig={"MaximumConcurrency": 4}
        )

    events = session.client("events")
    _ensure_indexer_schedule(events, client, configs["indexer"]["FunctionArn"])
    for name, schedule in (
        ("vieclambot-scraper-schedule", "cron(0 */6 * * ? *)"),
        ("vieclambot-matcher-schedule", "cron(20 */6 * * ? *)"),
        ("vieclambot-indexer-schedule", INDEXER_SCHEDULE),
    ):
        if not events.list_targets_by_rule(Rule=name)["Targets"]:
            raise RuntimeError(f"Missing scheduled target: {name}")
        events.put_rule(Name=name, ScheduleExpression=schedule, State="ENABLED")
        print(json.dumps({"rule": name, "schedule": schedule}), flush=True)
    # Build the first search index now (no user-visible effect).
    client.invoke(
        FunctionName="vieclambot-indexer", InvocationType="Event", Payload=b'{"force": true}'
    )
    print(
        "Deployment verified; TTL and queue retries configured. No test messages sent.", flush=True
    )


INDEXER_SCHEDULE = "cron(30 * * * ? *)"


def _ensure_indexer(client, package: bytes, matcher_conf: dict) -> dict:
    """Create ``vieclambot-indexer`` (role and environment copied from the matcher)."""
    name = "vieclambot-indexer"
    try:
        return client.get_function_configuration(FunctionName=name)
    except client.exceptions.ResourceNotFoundException:
        pass
    client.create_function(
        FunctionName=name,
        Runtime="python3.12",
        Architectures=["x86_64"],
        Role=matcher_conf["Role"],
        Handler="lambdas.indexer_handler.handler",
        Code={"ZipFile": package},
        Description="Builds the hourly search index (SQLite FTS5) from DynamoDB into S3",
        Timeout=300,
        MemorySize=1024,
        EphemeralStorage={"Size": 1024},
        Environment={"Variables": dict(matcher_conf.get("Environment", {}).get("Variables", {}))},
    )
    client.get_waiter("function_active_v2").wait(FunctionName=name)
    print(json.dumps({"function": name, "status": "created"}), flush=True)
    return client.get_function_configuration(FunctionName=name)


def _ensure_indexer_schedule(events, client, function_arn: str) -> None:
    rule = events.put_rule(
        Name="vieclambot-indexer-schedule",
        ScheduleExpression=INDEXER_SCHEDULE,
        State="ENABLED",
        Description="Rebuilds the search index every hour",
    )
    events.put_targets(
        Rule="vieclambot-indexer-schedule",
        Targets=[{"Id": "indexer-target", "Arn": function_arn}],
    )
    try:
        client.add_permission(
            FunctionName="vieclambot-indexer",
            StatementId="EventBridgeIndexerInvoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule["RuleArn"],
        )
    except client.exceptions.ResourceConflictException:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    deploy(args.profile)
