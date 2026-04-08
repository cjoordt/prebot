"""
integrations/strava.py — Strava OAuth2 client

Responsibilities:
- Refresh the access token using the stored refresh token
- Fetch up to 4 weeks of run activities from the Strava API
- Classify each activity's effort level and compute training load
- Cache results to data/strava_cache.json to avoid redundant API calls
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN")

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"

CACHE_FILE = Path(__file__).parent.parent / "data" / "strava_cache.json"

# Effort classification thresholds (average HR in bpm)
HR_EASY_MAX = 140
HR_MODERATE_MAX = 160

# Per-effort training load multipliers (from spec)
EFFORT_MULTIPLIERS = {
    "easy": 1.0,
    "moderate": 1.3,
    "hard": 1.6,
}

# Strava workout_type codes for runs
# 0=default, 1=race, 2=long run, 3=workout
WORKOUT_TYPE_EFFORT = {
    1: "hard",   # race
    3: "hard",   # workout / intervals
    2: "moderate",  # long run
    0: None,     # use HR fallback
}


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def refresh_access_token() -> str:
    """Exchange the stored refresh token for a fresh access token."""
    if not all([STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN]):
        raise ValueError(
            "STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, and STRAVA_REFRESH_TOKEN "
            "must all be set in .env"
        )

    resp = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": STRAVA_REFRESH_TOKEN,
        },
        timeout=10,
    )
    resp.raise_for_status()
    token_data = resp.json()
    logger.info("Strava access token refreshed successfully.")
    return token_data["access_token"]


# ---------------------------------------------------------------------------
# Effort classification
# ---------------------------------------------------------------------------

def classify_effort(activity: dict) -> str:
    """
    Classify a run as 'easy', 'moderate', or 'hard'.

    Priority:
    1. workout_type (race / workout → hard, long run → moderate)
    2. average_heartrate bands
    3. Fallback: moderate
    """
    workout_type = activity.get("workout_type") or 0
    effort = WORKOUT_TYPE_EFFORT.get(workout_type)

    if effort is not None:
        return effort

    hr = activity.get("average_heartrate")
    if hr is not None:
        if hr < HR_EASY_MAX:
            return "easy"
        elif hr < HR_MODERATE_MAX:
            return "moderate"
        else:
            return "hard"

    return "moderate"  # safe default


def compute_load(distance_meters: float, effort: str) -> float:
    """Return training load = distance_miles * effort_multiplier."""
    distance_miles = distance_meters / 1609.344
    multiplier = EFFORT_MULTIPLIERS[effort]
    return round(distance_miles * multiplier, 2)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {"fetched_at": None, "activities": []}


def _save_cache(data: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Strava cache updated: {len(data['activities'])} activities.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_recent_activities(weeks: int = 4, force_refresh: bool = False) -> list[dict]:
    """
    Return a list of enriched run activities from the last `weeks` weeks.

    Each activity dict contains the original Strava fields plus:
      - effort: "easy" | "moderate" | "hard"
      - load: float  (distance_miles * effort_multiplier)
      - date: "YYYY-MM-DD"

    Results are cached to strava_cache.json; set force_refresh=True to bypass.
    """
    cache = _load_cache()

    # Use cache if it was populated within the last hour and not forced
    if not force_refresh and cache.get("fetched_at"):
        age_seconds = time.time() - cache["fetched_at"]
        if age_seconds < 3600:
            logger.info("Returning Strava data from cache.")
            return cache["activities"]

    access_token = refresh_access_token()

    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    cutoff_epoch = int(cutoff.timestamp())

    activities = []
    page = 1

    while True:
        resp = requests.get(
            STRAVA_ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "after": cutoff_epoch,
                "per_page": 100,
                "page": page,
            },
            timeout=10,
        )
        resp.raise_for_status()
        page_data = resp.json()

        if not page_data:
            break

        for act in page_data:
            # Only include runs (type "Run" or sport_type "Run")
            if act.get("sport_type", act.get("type", "")) != "Run":
                continue

            effort = classify_effort(act)
            load = compute_load(act.get("distance", 0), effort)
            start_dt = datetime.fromisoformat(
                act["start_date_local"].replace("Z", "+00:00")
            )

            activities.append({
                "id": act["id"],
                "name": act.get("name", ""),
                "date": start_dt.strftime("%Y-%m-%d"),
                "distance_meters": act.get("distance", 0),
                "distance_miles": round(act.get("distance", 0) / 1609.344, 2),
                "moving_time_seconds": act.get("moving_time", 0),
                "elevation_gain_meters": act.get("total_elevation_gain", 0),
                "average_heartrate": act.get("average_heartrate"),
                "workout_type": act.get("workout_type"),
                "effort": effort,
                "load": load,
            })

        page += 1

    # Sort chronologically
    activities.sort(key=lambda a: a["date"])

    cache = {
        "fetched_at": time.time(),
        "activities": activities,
    }
    _save_cache(cache)
    return activities


def get_today_activities() -> list[dict]:
    """Return any activities logged today (uses cache, refreshes if stale)."""
    today = datetime.now().strftime("%Y-%m-%d")
    all_activities = fetch_recent_activities(force_refresh=True)
    return [a for a in all_activities if a["date"] == today]


def get_activities_by_date(date_str: str) -> list[dict]:
    """Return cached activities for a specific YYYY-MM-DD date."""
    all_activities = fetch_recent_activities()
    return [a for a in all_activities if a["date"] == date_str]
