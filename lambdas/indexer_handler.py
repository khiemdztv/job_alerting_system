"""
Lambda Handler: Search Indexer

Scheduled hourly (EventBridge ``vieclambot-indexer-schedule``).  Scans the jobs
table once and uploads the prebuilt search index to
``s3://<data-lake>/index/jobs.sqlite.gz`` (or ``jobs.pickle.gz`` when the runtime's
sqlite3 lacks FTS5).  Invoke with ``{"force": true}`` to rebuild even when the
current index is younger than ``VIECLAMBOT_SEARCH_INDEX_MIN_INTERVAL_SECONDS``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.logger import get_logger
from src.search_index.builder import build_and_upload

logger = get_logger(__name__)


def handler(event, context):
    event = event or {}
    force = bool(event.get("force")) if isinstance(event, dict) else False
    logger.info("Starting search index build (force=%s)", force)
    return build_and_upload(force=force)
