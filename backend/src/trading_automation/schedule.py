from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
SEOUL = ZoneInfo("Asia/Seoul")


def schedule_state(now: datetime) -> dict[str, dict[str, object]]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    eastern, seoul = now.astimezone(EASTERN), now.astimezone(SEOUL)
    # Five minute delay ensures the regular session close has settled. ZoneInfo
    # handles EST/EDT conversion without fixed UTC offsets.
    return {
        "us": {"local_date": eastern.date().isoformat(), "due": eastern.weekday() < 5 and eastern.time() >= time(16, 5),
               "timezone": "America/New_York", "after": "16:05"},
        "crypto": {"local_date": seoul.date().isoformat(), "due": seoul.time() >= time(9, 0),
                   "timezone": "Asia/Seoul", "after": "09:00"},
    }
