"""Monitoring-health classification (pure; standard library + schedule helpers).

Turns a product's raw monitoring state (last success, last attempt, failure
count, schedule) into a single status the UI can badge: ok / stale / failing /
paused / off / new. Kept dependency-free so it can be unit-tested offline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services import schedule

# Consecutive failed attempts before a listing/product is flagged as "failing".
FAIL_THRESHOLD = 2


@dataclass
class HealthStatus:
    status: str                       # ok | stale | failing | paused | off | new
    stale: bool = False
    failing: bool = False
    consecutive_failures: int = 0
    next_run: datetime | None = None
    last_checked: datetime | None = None
    last_error: str | None = None


def staleness_limit_minutes(kind: str | None, interval_minutes, default_interval: int) -> int:
    """How old a successful check may get before it counts as stale."""
    if (kind or "interval") == "daily":
        return 60 * 24 * 2          # two days
    interval = max(1, int(interval_minutes or default_interval))
    return max(int(interval * 2.5), interval + 10)


def classify(now: datetime, *, monitored: bool, has_active_url: bool, paused: bool,
             last_checked: datetime | None, last_attempt: datetime | None,
             consecutive_failures: int, kind: str | None, interval_minutes,
             daily_time: str | None, default_interval: int,
             last_error: str | None = None) -> HealthStatus:
    cf = int(consecutive_failures or 0)
    base = HealthStatus(status="ok", consecutive_failures=cf,
                        last_checked=last_checked, last_error=last_error)

    if not monitored:
        base.status = "off"
        return base
    if paused or not has_active_url:
        base.status = "paused"
        return base

    base.next_run = schedule.next_run(
        now, kind, interval_minutes, daily_time,
        last_attempt or last_checked, default_interval)

    if cf >= FAIL_THRESHOLD:
        base.status = "failing"
        base.failing = True
        return base

    if last_checked is None:
        # Monitored and active but no successful check yet.
        base.status = "new"
        base.stale = True
        return base

    limit = staleness_limit_minutes(kind, interval_minutes, default_interval)
    age_min = (now - last_checked).total_seconds() / 60.0
    if age_min > limit:
        base.status = "stale"
        base.stale = True
    return base
