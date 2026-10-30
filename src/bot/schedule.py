"""When a user's alert digest is due (frequency + quiet hours), in Việt Nam time.

The matcher runs every ``alert_interval_hours``.  Non-instant frequencies have
fixed delivery slots (08:00, 20:00, Monday 08:00 VN); a run delivers when it is
the run closest to a slot *or any later run* that finds the slot still undelivered
(``last_alert_at`` older than the slot).  That way quiet hours or a failed run
only defer a digest and never drop it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

VN_TZ = timezone(timedelta(hours=7))
QUIET_START = int(os.environ.get("VIECLAMBOT_QUIET_HOURS_START", "22"))
QUIET_END = int(os.environ.get("VIECLAMBOT_QUIET_HOURS_END", "7"))
DIGEST_HOUR = int(os.environ.get("VIECLAMBOT_DIGEST_HOUR", "8"))

_SLOT_HOURS = {
    "twice": (DIGEST_HOUR, (DIGEST_HOUR + 12) % 24),
    "daily": (DIGEST_HOUR,),
    "weekly": (DIGEST_HOUR,),
}
# How far back alerts look for still-unsent jobs, per frequency (days).
LOOKBACK_DAYS = {"instant": 0, "twice": 1, "daily": 2, "weekly": 8}


def quiet_window_label() -> str:
    return f"{QUIET_START:02d}:00–{QUIET_END:02d}:00"


def in_quiet_hours(now: datetime) -> bool:
    hour = now.astimezone(VN_TZ).hour
    if QUIET_START == QUIET_END:
        return False
    if QUIET_START > QUIET_END:
        return hour >= QUIET_START or hour < QUIET_END
    return QUIET_START <= hour < QUIET_END


def last_slot(frequency: str, now: datetime, interval_hours: float) -> datetime | None:
    """Latest delivery slot this run is responsible for (None for instant)."""
    hours = _SLOT_HOURS.get(frequency)
    if not hours:
        return None
    # A run "owns" a slot from half an interval before it, so with a 6 h cadence
    # exactly one run — the one closest to 08:00 — is the first to see it.
    anchor = (now + timedelta(hours=interval_hours / 2)).astimezone(VN_TZ)
    best = None
    for days_back in range(8):
        day = anchor.date() - timedelta(days=days_back)
        if frequency == "weekly" and day.weekday() != 0:
            continue
        for hour in hours:
            slot = datetime(day.year, day.month, day.day, hour, tzinfo=VN_TZ)
            if slot <= anchor and (best is None or slot > best):
                best = slot
        if best is not None:
            return best
    return best


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_due(prefs: dict, now: datetime, interval_hours: float) -> bool:
    if prefs.get("quiet_hours") and in_quiet_hours(now):
        return False
    slot = last_slot(str(prefs.get("frequency") or "instant"), now, interval_hours)
    if slot is None:
        return True
    last = _parse(prefs.get("last_alert_at"))
    return last is None or last < slot - timedelta(hours=interval_hours / 2)
