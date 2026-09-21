"""Scheduled alert entry point; supports a dry_run event flag."""
from src.matcher.alerts import run_alerts


def handler(event, context):
    return run_alerts(event or {}, context)
