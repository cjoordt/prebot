"""
utils.py — Shared timezone utility

All display, routing, and scheduling logic should use local_now() instead of
datetime.now(). Railway runs in UTC; this ensures the bot always thinks in
Pacific time to match the athlete's iPhone.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def local_now() -> datetime:
    """Return the current datetime in America/Los_Angeles (Pacific time)."""
    return datetime.now(LOCAL_TZ)
