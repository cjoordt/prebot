"""
tools/planner.py — Weekly training plan generator

Responsibilities:
- Build context from fatigue scores, calendar tags, and recent Strava activities
- Call Claude to produce a structured 7-day plan
- Persist the plan to data/weekly_plan.json
- Support plan adjustments (natural language → updated JSON)
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

PLAN_FILE = Path(__file__).parent.parent / "data" / "weekly_plan.json"
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

MODEL = "claude-opus-4-5"

DEFAULT_TARGET_ELEVATION_FT = 4800
PROGRESSION_RATE = 0.05  # max 5% above actual base per week

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_plan() -> dict | None:
    """Return the current weekly plan from disk, or None if none exists."""
    if not PLAN_FILE.exists():
        return None
    with open(PLAN_FILE, "r") as f:
        return json.load(f)


def save_plan(plan: dict) -> None:
    """Persist a plan dict to weekly_plan.json."""
    with open(PLAN_FILE, "w") as f:
        json.dump(plan, f, indent=2)
    logger.info(f"Weekly plan saved for week of {plan.get('week_of')}.")


# ---------------------------------------------------------------------------
# Context formatters
# ---------------------------------------------------------------------------

def _format_calendar(schedule: dict[str, str]) -> str:
    lines = []
    for date_str, tag in sorted(schedule.items()):
        day_name = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
        lines.append(f"  {day_name} {date_str}: {tag}")
    return "\n".join(lines)


def _format_recent_activities(activities: list[dict]) -> str:
    if not activities:
        return "  No recent activities found."
    lines = []
    for act in activities[-20:]:  # last 20 is plenty of context
        lines.append(
            f"  {act['date']} — {act['distance_miles']:.1f}mi "
            f"| {act['effort']} | load {act['load']} "
            f"| gain {act.get('elevation_gain_meters', 0):.0f}m"
            + (f" | HR {act['average_heartrate']:.0f}" if act.get("average_heartrate") else "")
        )
    return "\n".join(lines)


def _compute_base_mileage(activities: list[dict], weeks: int = 4) -> tuple[float, float]:
    """
    Compute actual weekly average and a modest target for next week.

    Returns (actual_weekly_avg, target_miles).
    """
    from datetime import date
    today = datetime.now().date()
    cutoff = today - timedelta(weeks=weeks)

    weekly_totals: dict[str, float] = {}
    for act in activities:
        act_date = datetime.strptime(act["date"], "%Y-%m-%d").date()
        if act_date < cutoff:
            continue
        # ISO week key
        week_key = act_date.strftime("%Y-W%W")
        weekly_totals[week_key] = weekly_totals.get(week_key, 0.0) + act["distance_miles"]

    if not weekly_totals:
        return 0.0, 15.0  # safe floor if no data

    avg = sum(weekly_totals.values()) / len(weekly_totals)
    target = round(avg * (1 + PROGRESSION_RATE), 1)
    return round(avg, 1), target


def _next_monday() -> str:
    today = datetime.now().date()
    days_until_monday = (7 - today.weekday()) % 7 or 7
    return (today + timedelta(days=days_until_monday)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------

def _call_claude(prompt: str) -> str:
    """Send a prompt to Claude and return the raw text response."""
    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def _parse_plan_json(raw: str) -> dict:
    """Extract and validate JSON from Claude's response."""
    # Strip any accidental markdown fences
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines
            if not line.startswith("```")
        )
    return json.loads(text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_weekly_plan(
    fatigue: dict,
    schedule: dict[str, str],
    recent_activities: list[dict],
) -> dict:
    """
    Generate a fresh weekly training plan via Claude.

    Args:
        fatigue: output of tools.fatigue.calculate_fatigue()
        schedule: output of integrations.calendar.fetch_week_schedule()
        recent_activities: output of integrations.strava.fetch_recent_activities()

    Returns the plan dict and saves it to weekly_plan.json.
    """
    template = (PROMPTS_DIR / "weekly_plan.txt").read_text()

    actual_weekly_avg, target_miles = _compute_base_mileage(recent_activities)

    prompt = template.format(
        atl=fatigue["atl"],
        ctl=fatigue["ctl"],
        form=fatigue["form"],
        recommendation=fatigue["recommendation"],
        calendar=_format_calendar(schedule),
        recent_activities=_format_recent_activities(recent_activities),
        week_of=_next_monday(),
        actual_weekly_avg=actual_weekly_avg,
        target_miles=target_miles,
        target_elevation_ft=DEFAULT_TARGET_ELEVATION_FT,
    )

    logger.info("Requesting weekly plan from Claude...")
    raw = _call_claude(prompt)

    plan = _parse_plan_json(raw)
    save_plan(plan)
    return plan


def adjust_plan(adjustment_text: str) -> dict:
    """
    Accept a natural language adjustment request and update the current plan.

    Example: "Move Saturday's long run to Sunday, I have a kid's soccer game"

    Returns the updated plan and saves it.
    """
    current_plan = load_plan()
    if not current_plan:
        raise ValueError("No current plan found. Generate a plan first.")

    prompt = (
        "You are an ultramarathon coach. The athlete has requested an adjustment "
        "to their weekly training plan.\n\n"
        "Current plan:\n"
        f"{json.dumps(current_plan, indent=2)}\n\n"
        "Athlete's request:\n"
        f"{adjustment_text}\n\n"
        "Apply the adjustment, preserve all unchanged days exactly as-is, "
        "and respond ONLY with the updated plan as valid JSON in the same schema. "
        "No prose, no markdown, no code fences."
    )

    logger.info(f"Adjusting plan: {adjustment_text!r}")
    raw = _call_claude(prompt)

    updated_plan = _parse_plan_json(raw)
    save_plan(updated_plan)
    return updated_plan


def format_plan_for_telegram(plan: dict) -> str:
    """
    Render the weekly plan as a concise Telegram message (≤150 words).
    """
    week_of = plan.get("week_of", "")
    target_miles = plan.get("target_miles", "?")
    target_elev = plan.get("target_elevation_ft", "?")
    days = plan.get("days", {})

    day_order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    day_labels = {
        "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
        "fri": "Fri", "sat": "Sat", "sun": "Sun",
    }

    lines = [f"Week of {week_of} — {target_miles}mi / {target_elev:,}ft\n"]

    for day_key in day_order:
        day = days.get(day_key, {})
        dtype = day.get("type", "rest")
        miles = day.get("miles")
        notes = day.get("notes", day.get("reason", ""))
        elev = day.get("elevation_ft")

        parts = [f"{day_labels[day_key]}: {dtype}"]
        if miles:
            parts.append(f"{miles}mi")
        if elev:
            parts.append(f"{elev:,}ft")
        if notes:
            parts.append(f"({notes})")

        lines.append(" ".join(parts))

    lines.append(f"\nLooks good? Reply yes to confirm or tell me what to change.")
    return "\n".join(lines)
