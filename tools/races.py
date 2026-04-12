"""
tools/races.py — Race registration and periodization

Handles:
- CRUD for races in data/races.json
- Periodization phase calculation from A-race date
- Natural language race intent extraction via Claude
- Vert target computation per phase
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

RACES_FILE = Path(__file__).parent.parent / "data" / "races.json"
MODEL = "claude-opus-4-5"

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ---------------------------------------------------------------------------
# Phase thresholds (weeks from race date)
# ---------------------------------------------------------------------------

TAPER_WEEKS = 3
RACE_SPECIFIC_WEEKS = 8
STRENGTH_WEEKS = 12

PHASE_DESCRIPTIONS = {
    "base": "Base building — easy volume, aerobic base, no intensity work",
    "strength": "Strength & hills — hill repeats, tempo, increasing vert",
    "race_specific": "Race-specific — race-pace long runs, back-to-back weekends, peak mileage",
    "taper": "Taper — volume reduction, 1-2 sharpening efforts, prioritize rest",
    "post_race": "Post-race — recovery and transition",
    "general": "General base building — no race registered",
}

# Vert multipliers relative to peak (race_specific) weekly vert
PHASE_VERT_MULTIPLIERS = {
    "base": 0.60,
    "strength": 1.00,
    "race_specific": 0.90,
    "taper": 0.50,
    "post_race": 0.40,
    "general": 0.65,
}

# Default peak weekly vert when race has no elevation data
DEFAULT_PEAK_VERT_FT = 5000

# Keywords that suggest the message is about managing races
RACE_KEYWORDS = {
    "race", "50k", "50-k", "marathon", "25k", "100k", "100 mile", "50 mile",
    "miler", "ultra", "signed up", "registered", "new race", "drop the race",
    "not doing", "remove race", "cancel race", "upcoming race",
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_races() -> list[dict]:
    if not RACES_FILE.exists():
        return []
    with open(RACES_FILE, "r") as f:
        data = json.load(f)
    return data.get("races", [])


def save_races(races: list[dict]) -> None:
    with open(RACES_FILE, "w") as f:
        json.dump({"races": races}, f, indent=2)
    logger.info(f"Saved {len(races)} races.")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_upcoming_races() -> list[dict]:
    """Return all races with future dates, sorted by date ascending."""
    today = datetime.now().date().isoformat()
    races = [r for r in load_races() if r.get("date", "") >= today]
    return sorted(races, key=lambda r: r["date"])


def get_active_race() -> dict | None:
    """Return the first upcoming A-priority race, or the first upcoming race."""
    upcoming = get_upcoming_races()
    if not upcoming:
        return None
    a_races = [r for r in upcoming if r.get("priority", "A") == "A"]
    return a_races[0] if a_races else upcoming[0]


# ---------------------------------------------------------------------------
# Phase calculation
# ---------------------------------------------------------------------------

def calculate_phase(race_date_str: str) -> dict:
    """
    Calculate the current training phase based on weeks to race.

    Returns:
        {
            "phase": "base" | "strength" | "race_specific" | "taper" | "post_race",
            "weeks_to_race": float,
            "description": str,
            "vert_multiplier": float,
        }
    """
    race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
    today = datetime.now().date()
    days_to_race = (race_date - today).days
    weeks_to_race = days_to_race / 7.0

    if weeks_to_race < 0:
        phase = "post_race"
    elif weeks_to_race <= TAPER_WEEKS:
        phase = "taper"
    elif weeks_to_race <= RACE_SPECIFIC_WEEKS:
        phase = "race_specific"
    elif weeks_to_race <= STRENGTH_WEEKS:
        phase = "strength"
    else:
        phase = "base"

    return {
        "phase": phase,
        "weeks_to_race": round(max(weeks_to_race, 0.0), 1),
        "description": PHASE_DESCRIPTIONS[phase],
        "vert_multiplier": PHASE_VERT_MULTIPLIERS[phase],
    }


def get_phase_context() -> dict:
    """
    Return the full phase context for the active A-race.
    Falls back to 'general' if no race is registered.
    """
    race = get_active_race()
    if not race:
        return {
            "phase": "general",
            "weeks_to_race": None,
            "description": PHASE_DESCRIPTIONS["general"],
            "vert_multiplier": PHASE_VERT_MULTIPLIERS["general"],
            "race_name": None,
            "race_date": None,
            "race_distance_miles": None,
            "race_elevation_gain_ft": None,
        }

    phase_info = calculate_phase(race["date"])
    phase_info.update({
        "race_name": race["name"],
        "race_date": race["date"],
        "race_distance_miles": race.get("distance_miles"),
        "race_elevation_gain_ft": race.get("elevation_gain_ft"),
    })
    return phase_info


# ---------------------------------------------------------------------------
# Vert targets
# ---------------------------------------------------------------------------

def compute_vert_target(phase_context: dict) -> int:
    """
    Compute weekly vert target in feet based on phase and race data.

    If the race has known elevation, peaks at 120% of race vert during
    strength/race-specific phases. Otherwise uses DEFAULT_PEAK_VERT_FT.
    """
    race_vert = phase_context.get("race_elevation_gain_ft")
    if race_vert:
        peak_weekly_vert = int(race_vert * 1.2)
    else:
        peak_weekly_vert = DEFAULT_PEAK_VERT_FT

    multiplier = phase_context.get("vert_multiplier", 0.65)
    return int(peak_weekly_vert * multiplier)


# ---------------------------------------------------------------------------
# Race parsing via Claude
# ---------------------------------------------------------------------------

def _looks_like_race_message(text: str) -> bool:
    """Quick heuristic check before calling Claude."""
    lower = text.lower()
    return any(kw in lower for kw in RACE_KEYWORDS)


def parse_race_intent(user_text: str) -> dict | None:
    """
    Use Claude to extract race management intent from a user message.

    Returns a dict with 'intent' and relevant fields, or None if not race-related.

    Intent values:
        "add"    — user is registering a new race
        "update" — user is updating an existing race field
        "remove" — user is dropping a race
        "none"   — message is not race management
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        "You are a race data extractor for a running coach bot. Analyze this message "
        "from an athlete and determine if it is about managing their race calendar.\n\n"
        f'Today\'s date: {today_str}\n'
        f'Message: "{user_text}"\n\n'
        "Respond ONLY with valid JSON, no prose, no markdown fences.\n\n"
        "If registering a new race:\n"
        '{"intent": "add", "name": "<race name>", "date": "<YYYY-MM-DD>", '
        '"distance_miles": <number>, "elevation_gain_ft": <number or null>, '
        '"course_notes": "<string or null>", "goal": "<string or null>", '
        '"priority": "<A|B|C or null>"}\n\n'
        "If updating an existing race:\n"
        '{"intent": "update", "name": "<race name>", '
        '"updates": {"<field>": <new value>}}\n\n'
        "If removing/dropping a race:\n"
        '{"intent": "remove", "name": "<race name>"}\n\n'
        "If not about managing races:\n"
        '{"intent": "none"}\n\n'
        "Conversion notes:\n"
        "- 50k = 31.1 miles, 25k = 15.5 miles, 100k = 62.1 miles\n"
        "- Convert km to miles (1km = 0.621 mi) if needed\n"
        "- If no year given for the date, use the next upcoming occurrence\n"
        "- Only return 'add' if a specific race name AND date are mentioned\n"
        "- priority null means the bot will assign a default"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(line for line in raw.splitlines() if not line.startswith("```"))
        result = json.loads(raw)
        if result.get("intent") == "none":
            return None
        return result
    except Exception as e:
        logger.warning(f"Race intent parse failed: {e}")
        return None


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

def add_race(race_data: dict) -> dict:
    """
    Add or replace a race. If a race with the same name already exists, replace it.
    Assigns priority automatically if not provided.
    Returns the saved race dict.
    """
    races = load_races()
    name_lower = race_data["name"].lower()

    # Remove existing race with same name (case-insensitive)
    races = [r for r in races if r["name"].lower() != name_lower]

    # Default priority: A if no A-race exists, B otherwise
    if not race_data.get("priority"):
        existing_a = any(r.get("priority") == "A" for r in races)
        race_data["priority"] = "B" if existing_a else "A"

    races.append(race_data)
    save_races(races)
    logger.info(f"Race added: {race_data['name']} on {race_data['date']}")
    return race_data


def update_race(name_query: str, updates: dict) -> bool:
    """Update fields on a race matched by name. Returns True if found."""
    races = load_races()
    name_lower = name_query.lower()

    for i, race in enumerate(races):
        if name_lower in race["name"].lower():
            races[i].update(updates)
            save_races(races)
            logger.info(f"Race updated: {race['name']} → {updates}")
            return True

    logger.warning(f"Race not found for update: {name_query!r}")
    return False


def remove_race(name_query: str) -> bool:
    """Remove a race matched by name. Returns True if found."""
    races = load_races()
    name_lower = name_query.lower()
    original_count = len(races)
    races = [r for r in races if name_lower not in r["name"].lower()]

    if len(races) < original_count:
        save_races(races)
        logger.info(f"Race removed: {name_query!r}")
        return True

    logger.warning(f"Race not found for removal: {name_query!r}")
    return False


def log_race_result(name_query: str, result: dict) -> bool:
    """Record a post-race result on the matching race. Returns True if found."""
    races = load_races()
    name_lower = name_query.lower()

    for i, race in enumerate(races):
        if name_lower in race["name"].lower():
            races[i]["result"] = {
                **result,
                "logged_at": datetime.now().strftime("%Y-%m-%d"),
            }
            save_races(races)
            logger.info(f"Race result logged: {race['name']}")
            return True

    return False


# ---------------------------------------------------------------------------
# Context formatters
# ---------------------------------------------------------------------------

def format_races_for_context() -> str:
    """Format upcoming races for the agent context block."""
    upcoming = get_upcoming_races()
    if not upcoming:
        return "  No races registered."

    lines = []
    for race in upcoming:
        weeks_to = (
            datetime.strptime(race["date"], "%Y-%m-%d").date() - datetime.now().date()
        ).days / 7
        priority = race.get("priority", "A")
        dist = race.get("distance_miles", "?")
        elev = race.get("elevation_gain_ft")
        elev_str = f" | {int(elev):,}ft vert" if elev else ""
        line = (
            f"  [{priority}] {race['name']} — {race['date']} "
            f"({weeks_to:.0f} wks) | {dist}mi{elev_str}"
        )
        if race.get("goal"):
            line += f" | goal: {race['goal']}"
        lines.append(line)

    return "\n".join(lines)


def format_phase_for_context() -> str:
    """Format the current training phase for the context block."""
    ctx = get_phase_context()
    if ctx["phase"] == "general":
        return "  General base building (no race registered)"

    return (
        f"  {ctx['description']} | "
        f"{ctx['weeks_to_race']:.0f} weeks to {ctx['race_name']}"
    )
