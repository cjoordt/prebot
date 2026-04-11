"""
integrations/health.py — Health data store

Receives weight and sleep data POSTed from an iPhone Shortcut
and persists it to data/health_log.json for the agent to read.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

HEALTH_LOG_FILE = Path(__file__).parent.parent / "data" / "health_log.json"


def load_health_log() -> list[dict]:
    if not HEALTH_LOG_FILE.exists():
        return []
    with open(HEALTH_LOG_FILE, "r") as f:
        data = json.load(f)
    return data.get("entries", [])


def save_health_entry(entry: dict) -> None:
    """Upsert today's health entry."""
    entries = load_health_log()
    date = entry.get("date", datetime.now().strftime("%Y-%m-%d"))
    entries = [e for e in entries if e.get("date") != date]
    entries.append(entry)
    # Keep last 90 days only
    entries = sorted(entries, key=lambda e: e.get("date", ""))[-90:]
    with open(HEALTH_LOG_FILE, "w") as f:
        json.dump({"entries": entries}, f, indent=2)
    logger.info(f"Health data saved for {date}: {entry}")


def get_todays_health() -> dict | None:
    today = datetime.now().strftime("%Y-%m-%d")
    for entry in load_health_log():
        if entry.get("date") == today:
            return entry
    return None


def get_recent_health(days: int = 7) -> list[dict]:
    entries = load_health_log()
    return entries[-days:] if entries else []
