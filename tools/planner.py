"""
tools/planner.py — Weekly training plan generator

Responsibilities:
- Build context from fatigue scores, calendar tags, recent Strava activities,
  periodization phase, and weather forecast
- Call Claude to produce a structured 7-day plan with typed workouts
- Persist the plan to data/weekly_plan.json
- Support plan adjustments (natural language → updated JSON)
- Validate plans for constraint violations (80/20, back-to-back, phase rules)
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

PROGRESSION_RATE = 0.05  # max 5% above actual base per week

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Workout types that count as "hard" for constraint checks
HARD_TYPES = {"tempo", "intervals", "hill_repeats", "race_pace_long"}

# Types forbidden per phase
PHASE_FORBIDDEN_TYPES: dict[str, set[str]] = {
    "base": {"intervals", "race_pace_long"},
    "taper": {"intervals", "hill_repeats"},
    "post_race": {"intervals", "tempo", "hill_repeats", "race_pace_long"},
    "general": {"intervals", "race_pace_long"},  # only easy/long/rest without a race
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_plan() -> dict | None:
    if not PLAN_FILE.exists():
        return None
    with open(PLAN_FILE, "r") as f:
        return json.load(f)


def save_plan(plan: dict) -> None:
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
    for act in activities[-20:]:
        lines.append(
            f"  {act['date']} — {act['distance_miles']:.1f}mi "
            f"| {act['effort']} | load {act['load']} "
            f"| gain {act.get('elevation_gain_meters', 0):.0f}m"
            + (f" | HR {act['average_heartrate']:.0f}" if act.get("average_heartrate") else "")
        )
    return "\n".join(lines)


def _format_weather_forecast(forecast: list[dict]) -> str:
    if not forecast:
        return "  Weather forecast unavailable."
    lines = []
    for day in forecast[:7]:
        cat = day.get("category", "?")
        temp = day.get("temp_max_f", "?")
        wind = day.get("wind_mph", 0)
        precip = day.get("precip_mm", 0)
        date_str = day.get("date", "")
        day_name = ""
        try:
            day_name = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a")
        except ValueError:
            pass
        lines.append(f"  {day_name} {date_str}: {temp}°F, {cat}, {wind:.0f}mph wind, {precip:.1f}mm precip")
    return "\n".join(lines)


def _compute_base_mileage(activities: list[dict], weeks: int = 4) -> tuple[float, float]:
    """
    Compute actual weekly average and a modest target for next week.
    Returns (actual_weekly_avg, target_miles).
    """
    today = datetime.now().date()
    cutoff = today - timedelta(weeks=weeks)

    weekly_totals: dict[str, float] = {}
    for act in activities:
        act_date = datetime.strptime(act["date"], "%Y-%m-%d").date()
        if act_date < cutoff:
            continue
        week_key = act_date.strftime("%Y-W%W")
        weekly_totals[week_key] = weekly_totals.get(week_key, 0.0) + act["distance_miles"]

    if not weekly_totals:
        return 0.0, 15.0  # safe floor

    avg = sum(weekly_totals.values()) / len(weekly_totals)
    target = round(avg * (1 + PROGRESSION_RATE), 1)
    return round(avg, 1), target


def _next_monday() -> str:
    today = datetime.now().date()
    days_until_monday = (7 - today.weekday()) % 7 or 7
    return (today + timedelta(days=days_until_monday)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------

def validate_plan(plan: dict, phase: str) -> list[str]:
    """
    Check a plan dict for constraint violations. Returns a list of warning strings.
    Empty list means the plan passes all checks.

    Checks:
    - No back-to-back hard days
    - Hard intensity does not exceed ~25% of total miles (80/20 rule with margin)
    - No workout types forbidden for the current phase
    """
    warnings: list[str] = []
    days = plan.get("days", {})
    day_order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    # Back-to-back hard days
    prev_hard = False
    for day_key in day_order:
        day = days.get(day_key, {})
        dtype = day.get("type", "rest")
        is_hard = dtype in HARD_TYPES
        if is_hard and prev_hard:
            warnings.append(f"Back-to-back hard days at {day_key}")
        prev_hard = is_hard

    # 80/20 rule (allow up to 25% hard with a small margin)
    hard_miles = sum(
        (days[k].get("miles") or 0)
        for k in days
        if days[k].get("type") in HARD_TYPES
    )
    total_miles = sum((days[k].get("miles") or 0) for k in days)
    if total_miles > 0:
        hard_ratio = hard_miles / total_miles
        if hard_ratio > 0.25:
            warnings.append(
                f"Hard intensity is {hard_ratio:.0%} of total miles (target ≤20%)"
            )

    # Phase-appropriate types
    forbidden = PHASE_FORBIDDEN_TYPES.get(phase, set())
    for day_key, day in days.items():
        dtype = day.get("type", "rest")
        if dtype in forbidden:
            warnings.append(f"{day_key}: '{dtype}' is not appropriate in {phase} phase")

    return warnings


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------

def _call_claude(prompt: str) -> str:
    message = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def _parse_plan_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```"))
    return json.loads(text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_weekly_plan(
    fatigue: dict,
    schedule: dict[str, str],
    recent_activities: list[dict],
    phase_context: dict | None = None,
    weather_forecast: list[dict] | None = None,
) -> dict:
    """
    Generate a fresh weekly training plan via Claude.

    Args:
        fatigue: output of tools.fatigue.calculate_fatigue()
        schedule: output of integrations.calendar.fetch_week_schedule()
        recent_activities: output of integrations.strava.fetch_recent_activities()
        phase_context: output of tools.races.get_phase_context() (optional)
        weather_forecast: output of integrations.weather.get_week_weather_forecast() (optional)

    Returns the plan dict and saves it to weekly_plan.json.
    """
    if phase_context is None:
        from tools.races import get_phase_context
        phase_context = get_phase_context()

    from tools.races import compute_vert_target
    vert_target = compute_vert_target(phase_context)

    template = (PROMPTS_DIR / "weekly_plan.txt").read_text()

    actual_weekly_avg, target_miles = _compute_base_mileage(recent_activities)

    # Taper override: reduce target mileage during taper
    phase = phase_context.get("phase", "general")
    if phase == "taper":
        weeks_to = phase_context.get("weeks_to_race", 3)
        if weeks_to <= 1:
            target_miles = round(actual_weekly_avg * 0.60, 1)
        elif weeks_to <= 2:
            target_miles = round(actual_weekly_avg * 0.75, 1)
        else:
            target_miles = round(actual_weekly_avg * 0.85, 1)

    # Build weather block
    weather_block = _format_weather_forecast(weather_forecast or [])

    # Build phase block
    race_name = phase_context.get("race_name") or "no race registered"
    race_date = phase_context.get("race_date") or "N/A"
    weeks_to_race = phase_context.get("weeks_to_race")
    weeks_str = f"{weeks_to_race:.0f} weeks" if weeks_to_race is not None else "N/A"
    phase_desc = phase_context.get("description", "")

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
        target_elevation_ft=vert_target,
        phase=phase,
        phase_description=phase_desc,
        race_name=race_name,
        race_date=race_date,
        weeks_to_race=weeks_str,
        weather_forecast=weather_block,
    )

    logger.info(f"Requesting weekly plan from Claude (phase={phase}, vert={vert_target}ft)...")
    raw = _call_claude(prompt)
    plan = _parse_plan_json(raw)
    save_plan(plan)

    # Log any constraint violations (non-fatal — Claude sometimes bends rules)
    violations = validate_plan(plan, phase)
    if violations:
        logger.warning(f"Plan violations: {violations}")

    return plan


def adjust_plan(adjustment_text: str) -> dict:
    """
    Accept a natural language adjustment request and update the current plan.
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
    Render the weekly plan as a concise Telegram message.
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

    elev_str = f"{int(target_elev):,}ft" if isinstance(target_elev, (int, float)) else str(target_elev)
    lines = [f"Week of {week_of} — {target_miles}mi / {elev_str}\n"]

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
            parts.append(f"{int(elev):,}ft")
        if notes:
            parts.append(f"({notes})")

        lines.append(" ".join(parts))

    lines.append("\nLooks good? Reply yes to confirm or tell me what to change.")
    return "\n".join(lines)
