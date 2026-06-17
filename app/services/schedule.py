"""Pure scheduling helpers (standard library only, no app/ORM imports).

Kept dependency-free so the worker's schedule math can be unit-tested without a
database, APScheduler, or network.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def parse_hhmm(value: str | None, default: tuple[int, int] = (9, 0)) -> tuple[int, int]:
    """Parse 'HH:MM' to (hour, minute); fall back to ``default`` on anything odd."""
    try:
        hh, mm = (value or "").split(":")
        h, m = int(hh), int(mm)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:  # noqa: BLE001
        pass
    return default


def schedule_signature(kind: str | None, interval_minutes, daily_time: str | None,
                       default_interval: int) -> str:
    """Stable string identifying a product's schedule, used to detect changes."""
    if (kind or "interval") == "daily":
        h, m = parse_hhmm(daily_time)
        return f"daily:{h:02d}:{m:02d}"
    interval = max(1, int(interval_minutes or default_interval))
    return f"interval:{interval}"


def should_schedule(track_price: bool, track_stock: bool, has_active_url: bool) -> bool:
    """A product is scheduled only if it's monitored and has a live listing."""
    return bool((track_price or track_stock) and has_active_url)


def next_run(now: datetime, kind: str | None, interval_minutes, daily_time: str | None,
             last_run: datetime | None, default_interval: int) -> datetime:
    """Estimate the next scheduled check time (naive UTC).

    Interval schedules count forward from the last run (or now if never run);
    an overdue interval returns ``now``. Daily schedules return the next HH:MM.
    This mirrors the worker's triggers closely enough for display.
    """
    if (kind or "interval") == "daily":
        h, m = parse_hhmm(daily_time)
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    interval = max(1, int(interval_minutes or default_interval))
    nxt = (last_run or now) + timedelta(minutes=interval)
    return nxt if nxt > now else now
