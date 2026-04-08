"""
integrations/calendar.py — Google Calendar client

Responsibilities:
- Authenticate via OAuth2 user credentials (credentials.json + token.json)
- Fetch events for the next 7 days from the configured calendar
- Tag each day as: open / travel / busy-morning / busy-afternoon / blocked
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CREDENTIALS_PATH = Path(os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH", "credentials.json"))
TOKEN_PATH = CREDENTIALS_PATH.parent / "token.json"

# Full-access calendars (personal, family) — event titles/descriptions are readable.
_RAW_CALENDAR_IDS = os.getenv("GOOGLE_CALENDAR_IDS", os.getenv("GOOGLE_CALENDAR_ID", "primary"))
CALENDAR_IDS: list[str] = [c.strip() for c in _RAW_CALENDAR_IDS.split(",") if c.strip()]

# Free/busy-only calendars (e.g. work Workspace with external sharing locked down).
# These contribute busy time slots but no event details — no keyword matching.
_RAW_FREEBUSY_IDS = os.getenv("GOOGLE_FREEBUSY_CALENDAR_IDS", "")
FREEBUSY_CALENDAR_IDS: list[str] = [c.strip() for c in _RAW_FREEBUSY_IDS.split(",") if c.strip()]

# On Railway (or any headless env), store the token.json contents in this env var
# so the browser OAuth flow is never needed at runtime.
GOOGLE_TOKEN_JSON = os.getenv("GOOGLE_TOKEN_JSON")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Day tagging thresholds (local time, 24h)
# Morning = early training window (5–9am). Events after 9am don't block a morning run.
MORNING_START = 5    # 5am
MORNING_END = 9      # 9am — anything starting after this leaves time for an early run
AFTERNOON_START = 12 # noon
AFTERNOON_END = 18   # 6pm

# Keywords that signal a travel day (case-insensitive)
TRAVEL_KEYWORDS = {
    "flight", "fly", "travel", "airport", "hotel", "trip",
    "out of office", "ooo", "conference", "offsite",
}

# Keywords that signal a full-day block
BLOCKED_KEYWORDS = {
    "vacation", "holiday", "pto", "out of office", "ooo",
    "day off", "leave",
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_credentials() -> Credentials:
    """
    Load OAuth2 credentials, refreshing if needed.

    Resolution order:
    1. GOOGLE_TOKEN_JSON env var (Railway / headless deployment)
    2. token.json file on disk (local dev, written after first browser auth)
    3. Full browser OAuth flow (first local run only)
    """
    creds = None

    if GOOGLE_TOKEN_JSON:
        creds = Credentials.from_authorized_user_info(
            __import__("json").loads(GOOGLE_TOKEN_JSON), SCOPES
        )
    elif TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            logger.info("Google Calendar token refreshed.")
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Google Calendar credentials not found at {CREDENTIALS_PATH}. "
                    "Download credentials.json from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)
            logger.info("Google Calendar OAuth2 flow completed.")

        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return creds


def _build_service():
    """Return an authenticated Google Calendar service object."""
    creds = _get_credentials()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------

def _is_all_day(event: dict) -> bool:
    return "date" in event.get("start", {})


def _event_text(event: dict) -> str:
    """Combine summary + description for keyword matching."""
    return " ".join([
        event.get("summary", ""),
        event.get("description", "") or "",
    ]).lower()


def _contains_any(text: str, keywords: set) -> bool:
    return any(kw in text for kw in keywords)


def _event_hour_range(event: dict, date_str: str) -> tuple[int, int] | None:
    """
    Return (start_hour, end_hour) in local time for a timed event on date_str.
    Returns None for all-day events or events outside the given date.
    """
    start_raw = event.get("start", {}).get("dateTime")
    end_raw = event.get("end", {}).get("dateTime")
    if not start_raw or not end_raw:
        return None

    start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))

    if start_dt.strftime("%Y-%m-%d") != date_str:
        return None

    return start_dt.hour, end_dt.hour


# ---------------------------------------------------------------------------
# Day tagging
# ---------------------------------------------------------------------------

def _tag_day(date_str: str, events: list[dict]) -> str:
    """
    Apply tagging rules to a single day's events.

    Priority (first match wins):
      1. blocked  — all-day event with blocked keyword
      2. travel   — any event with travel keyword
      3. blocked  — 3+ hours of events before 9am (early schedule crush)
      4. busy-morning  — timed event between 5am–noon
      5. busy-afternoon — timed event between noon–6pm
      6. open
    """
    if not events:
        return "open"

    text_blocks = [_event_text(e) for e in events]

    # 1. All-day blocked keyword
    for event, text in zip(events, text_blocks):
        if _is_all_day(event) and _contains_any(text, BLOCKED_KEYWORDS):
            return "blocked"

    # 2. Travel keyword in any event
    for text in text_blocks:
        if _contains_any(text, TRAVEL_KEYWORDS):
            return "travel"

    # 3. Early-morning crush: sum timed event hours before 9am
    early_hours = 0.0
    for event in events:
        hr = _event_hour_range(event, date_str)
        if hr is None:
            continue
        start_h, end_h = hr
        if start_h < 9:
            early_hours += min(end_h, 9) - start_h
    if early_hours >= 3:
        return "blocked"

    # 4 & 5. Morning / afternoon
    has_morning = False
    has_afternoon = False

    for event in events:
        hr = _event_hour_range(event, date_str)
        if hr is None:
            # Untimed all-day events that aren't blocked/travel → skip
            continue
        start_h, end_h = hr
        if start_h < MORNING_END and end_h > MORNING_START:
            has_morning = True
        if start_h < AFTERNOON_END and end_h > AFTERNOON_START:
            has_afternoon = True

    if has_morning and has_afternoon:
        return "blocked"
    if has_morning:
        return "busy-morning"
    if has_afternoon:
        return "busy-afternoon"

    return "open"


# ---------------------------------------------------------------------------
# FreeBusy fetch (for calendars with restricted sharing)
# ---------------------------------------------------------------------------

def _fetch_freebusy_events(
    service,
    cal_ids: list[str],
    time_min: str,
    time_max: str,
) -> list[dict]:
    """
    Query the freeBusy API for one or more calendars and return a list of
    synthetic timed-event dicts (no title/description) compatible with _tag_day.
    """
    if not cal_ids:
        return []

    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": cal_id} for cal_id in cal_ids],
    }
    result = service.freebusy().query(body=body).execute()

    synthetic_events = []
    for cal_id in cal_ids:
        busy_slots = result.get("calendars", {}).get(cal_id, {}).get("busy", [])
        logger.info(f"FreeBusy {cal_id!r}: {len(busy_slots)} busy slots.")
        for slot in busy_slots:
            # Build a minimal event dict that _event_hour_range can parse
            synthetic_events.append({
                "start": {"dateTime": slot["start"]},
                "end": {"dateTime": slot["end"]},
            })

    return synthetic_events


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_week_schedule(days: int = 7) -> dict[str, str]:
    """
    Fetch the next `days` days from all configured calendars and return a dict of:
        { "YYYY-MM-DD": tag, ... }
    where tag is one of: open / travel / busy-morning / busy-afternoon / blocked

    Events from all calendars are merged before tagging, so a travel event on the
    family calendar and a busy-morning event on the work calendar both influence
    the same day's tag.
    """
    service = _build_service()

    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    # Initialise empty buckets for every day in the window
    events_by_date: dict[str, list] = {}
    for i in range(days):
        date_str = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        events_by_date[date_str] = []

    # Fetch busy slots from restricted calendars (no event details)
    freebusy_events = _fetch_freebusy_events(service, FREEBUSY_CALENDAR_IDS, time_min, time_max)
    for event in freebusy_events:
        start_raw = event["start"]["dateTime"]
        date_str = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        if date_str in events_by_date:
            events_by_date[date_str].append(event)

    # Fetch and merge events from every configured calendar
    for cal_id in CALENDAR_IDS:
        result = (
            service.events()
            .list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        raw_events = result.get("items", [])
        logger.info(f"Calendar {cal_id!r}: {len(raw_events)} events fetched.")

        for event in raw_events:
            start = event.get("start", {})
            if "date" in start:
                date_str = start["date"]
            elif "dateTime" in start:
                date_str = datetime.fromisoformat(start["dateTime"]).strftime("%Y-%m-%d")
            else:
                continue

            if date_str in events_by_date:
                events_by_date[date_str].append(event)

    # Tag each day
    schedule = {
        date_str: _tag_day(date_str, day_events)
        for date_str, day_events in events_by_date.items()
    }

    logger.info(f"Calendar fetched: {schedule}")
    return schedule
