"""
Structured logging for ViecLamBot.

Uses Python's built-in logging with JSON formatting for CloudWatch compatibility.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from src.config import get_settings


class JSONFormatter(logging.Formatter):
    """JSON log formatter for CloudWatch Logs."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields if present
        if hasattr(record, "source"):
            log_data["source"] = record.source
        if hasattr(record, "job_count"):
            log_data["job_count"] = record.job_count
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms
        if hasattr(record, "error_type"):
            log_data["error_type"] = record.error_type

        # Add exception info
        if record.exc_info and record.exc_info[1]:
            log_data["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Unknown",
                "message": str(record.exc_info[1]),
            }

        return json.dumps(log_data, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """Get a configured logger instance.

    Args:
        name: Logger name, typically __name__ of the calling module.

    Returns:
        Configured logger with JSON formatting.
    """
    settings = get_settings()
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)

    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    return logger
