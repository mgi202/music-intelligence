"""Night-window math (Europe/London).

The VPS clock is UTC; the window is defined in the user's local time so
"1 a.m. to 7 a.m." means the same thing year-round. All functions take an
aware UTC datetime and convert via zoneinfo — never compare against naive
datetime.now(), which silently drifts an hour across DST.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def window_hours() -> tuple[int, int]:
    """(start_hour, end_hour) of the night window, London local time."""
    return (
        int(os.getenv("NIGHT_WINDOW_START_HOUR", "1")),
        int(os.getenv("NIGHT_WINDOW_END_HOUR", "7")),
    )


def to_london(now_utc: datetime) -> datetime:
    if now_utc.tzinfo is None:
        raise ValueError("night-window math needs an aware UTC datetime")
    return now_utc.astimezone(LONDON)


def in_window(now_utc: datetime) -> bool:
    """True when the London hour is inside [start, end)."""
    start, end = window_hours()
    return start <= to_london(now_utc).hour < end


def london_date(now_utc: datetime) -> str:
    """The London calendar date (ISO) — the once-per-night gate key."""
    return to_london(now_utc).date().isoformat()


def window_start_utc(now_utc: datetime) -> datetime:
    """UTC datetime of the start of the night window covering/preceding now.

    Inside the window this is tonight's start; outside it is the start of the
    most recently begun window (used by the digest to bound "overnight").
    """
    start, _ = window_hours()
    local = to_london(now_utc)
    candidate = local.replace(hour=start, minute=0, second=0, microsecond=0)
    if candidate > local:
        candidate = (local - timedelta(days=1)).replace(
            hour=start, minute=0, second=0, microsecond=0
        )
    return candidate.astimezone(timezone.utc)


def minutes_until_window_start(now_utc: datetime) -> float:
    """Minutes from now until the NEXT window start (0 if inside the window)."""
    if in_window(now_utc):
        return 0.0
    start, _ = window_hours()
    local = to_london(now_utc)
    candidate = local.replace(hour=start, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate = (local + timedelta(days=1)).replace(
            hour=start, minute=0, second=0, microsecond=0
        )
    return max(0.0, (candidate - local).total_seconds() / 60.0)
