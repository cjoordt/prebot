"""
tools/parser.py — Natural language reply parser

Uses Claude to extract structured data from athlete check-in replies
and log entries to activity_log.json.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from utils import local_now

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

LOG_FILE = Path(__file__).parent.parent / "data" / "activity_log.json"
MODEL = "claude-opus-4-5"

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Log persistence
# ---------------------------------------------------------------------------

def load_log() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    with open(LOG_FILE, "r") as f:
        data = json.load(f)
    return data.get("entries", [])


def save_log(entries: list[dict]) -> None:
    with open(LOG_FILE, "w") as f:
        json.dump({"entries": entries}, f, indent=2)


def append_log_entry(entry: dict) -> None:
    entries = load_log()
    # Replace today's entry if it already exists
    today = local_now().strftime("%Y-%m-%d")
    entries = [e for e in entries if e.get("date") != today]
    entries.append(entry)
    save_log(entries)
    logger.info(f"Activity log updated for {entry.get('date')}.")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_checkin_reply(user_text: str, date: str | None = None) -> dict:
    """
    Parse a free-form check-in reply into a structured activity_log entry.

    Example input:  "slept 6.5hrs, had 2 drinks, legs feel heavy, stressed about work"
    Example output: {"date": "2026-04-03", "sleep_hours": 6.5, "sleep_quality": 2,
                     "alcohol_drinks": 2, "legs": "heavy", "stress": "high", ...}

    Returns the parsed dict AND appends it to activity_log.json.
    """
    today = date or local_now().strftime("%Y-%m-%d")

    prompt = (
        "Extract wellness metrics from this athlete check-in message. "
        "Respond ONLY with valid JSON — no prose, no markdown.\n\n"
        f'Message: "{user_text}"\n\n'
        "Return a JSON object with these fields (omit fields not mentioned):\n"
        "{\n"
        f'  "date": "{today}",\n'
        '  "sleep_hours": <number or null>,\n'
        '  "sleep_quality": <1-5 scale: 1=terrible, 5=great, or null>,\n'
        '  "alcohol_drinks": <number or null>,\n'
        '  "nutrition": <"poor"|"ok"|"good" or null>,\n'
        '  "legs": <"heavy"|"ok"|"fresh" or null>,\n'
        '  "stress": <"low"|"moderate"|"high" or null>,\n'
        '  "notes": "<any other relevant detail or null>"\n'
        "}\n\n"
        "Infer sleep_quality from descriptive words (e.g. 'woke up a lot' → 2, "
        "'slept great' → 5). If alcohol is mentioned but count is unclear, use 1."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines() if not line.startswith("```")
        )

    entry = json.loads(raw)
    entry["date"] = today  # ensure date is always set

    append_log_entry(entry)
    return entry


def get_todays_log() -> dict | None:
    """Return today's activity log entry, if it exists."""
    today = local_now().strftime("%Y-%m-%d")
    for entry in load_log():
        if entry.get("date") == today:
            return entry
    return None
