"""Timezone-aware display helpers.

All timestamps are stored naive in UTC (``datetime.utcnow()``). These helpers
convert them to the timezone configured on the Settings page right before
display, so the price chart, tables, and labels all show the user's local time
instead of raw UTC. ``pytz`` is used (not :mod:`zoneinfo`) because it bundles its
own tz database, so it works without the OS one being present.
"""
from __future__ import annotations

from datetime import datetime

import pytz

UTC = pytz.utc


def resolve_tz(name: str | None):
    """Configured IANA timezone, falling back to UTC for blank/invalid names."""
    if not name:
        return UTC
    try:
        return pytz.timezone(name)
    except Exception:  # noqa: BLE001 - pytz.UnknownTimeZoneError + non-str input
        return UTC


def tz_name(name: str | None) -> str:
    """Canonical, definitely-valid IANA name for a configured value (for JS Intl)."""
    return str(resolve_tz(name))


def to_zone(value: datetime | None, tz):
    """Treat a stored naive datetime as UTC and convert it to ``tz``."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = UTC.localize(value)
    return value.astimezone(tz)


def utc_iso(value: datetime | None) -> str | None:
    """Absolute ISO-8601 with an explicit UTC offset.

    Naive timestamps serialized with ``.isoformat()`` carry no offset, so client
    JS ``Date.parse`` reads them as *browser-local* time — shifting the chart by
    the viewer's UTC offset. Emitting an explicit offset fixes the instant; the
    chart then formats it in the configured zone via ``Intl`` ``timeZone``.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = UTC.localize(value)
    return value.astimezone(UTC).isoformat()
