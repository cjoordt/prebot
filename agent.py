"""
agent.py — Claude brain for Ultra Coach

Assembles full context and handles all message types:
  - Freeform messages (anytime)
  - Evening check-in initiation
  - Missed workout detection and flow
  - Plan adjustment after athlete reply
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from bot import append_message, load_conversation
from integrations.strava import fetch_recent_activities, get_today_activities
from integrations.calendar import fetch_week_schedule
from tools.fatigue import calculate_fatigue
from tools.parser import parse_checkin_reply, get_todays_log
from tools.planner import load_plan, adjust_plan, format_plan_for_telegram

load_dotenv()

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
MODEL = "claude-opus-4-5"
MAX_HISTORY_MESSAGES = 20

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _format_plan_summary(plan: dict | None) -> str:
    if not plan:
        return "No plan for this week yet."

    today_key = datetime.now().strftime("%a").lower()[:3]
    days = plan.get("days", {})
    lines = [f"Week of {plan.get('week_of')} — {plan.get('target_miles')}mi target"]

    for key, day in days.items():
        marker = " ← TODAY" if key == today_key else ""
        dtype = day.get("type", "rest")
        miles = day.get("miles", "")
        notes = day.get("notes", day.get("reason", ""))
        actual = plan.get("actuals", {}).get(key)
        actual_str = f" [actual: {actual}mi]" if actual else ""
        line = f"  {key}: {dtype}"
        if miles:
            line += f" {miles}mi"
        if notes:
            line += f" ({notes})"
        line += actual_str + marker
        lines.append(line)

    return "\n".join(lines)


def _format_todays_strava(activities: list[dict]) -> str:
    if not activities:
        return "No activity logged today."
    parts = []
    for act in activities:
        parts.append(
            f"{act['distance_miles']:.1f}mi | {act['effort']} effort | "
            f"gain {act.get('elevation_gain_meters', 0):.0f}m"
            + (f" | HR {act['average_heartrate']:.0f}" if act.get("average_heartrate") else "")
        )
    return "\n".join(parts)


def _format_calendar_summary(schedule: dict[str, str]) -> str:
    lines = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    for date_str, tag in sorted(schedule.items()):
        day_name = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a")
        marker = " ← today" if date_str == today_str else ""
        lines.append(f"  {day_name} {date_str}: {tag}{marker}")
    return "\n".join(lines)


def build_context_block() -> str:
    """
    Assemble a full dynamic context block injected into the system prompt.
    Pulls live data from Strava, Calendar, Fatigue model, and the current plan.
    """
    try:
        recent_activities = fetch_recent_activities(weeks=6)
    except Exception as e:
        logger.warning(f"Strava fetch failed: {e}")
        recent_activities = []

    try:
        today_activities = get_today_activities()
    except Exception as e:
        logger.warning(f"Today's Strava fetch failed: {e}")
        today_activities = []

    try:
        schedule = fetch_week_schedule(days=7)
    except Exception as e:
        logger.warning(f"Calendar fetch failed: {e}")
        schedule = {}

    fatigue = calculate_fatigue(recent_activities)
    plan = load_plan()

    return (
        "---\n"
        "## Live Context (updated each message)\n\n"
        f"**Date:** {datetime.now().strftime('%A, %B %d %Y')}\n\n"
        f"**Fatigue:**\n"
        f"  ATL: {fatigue['atl']} | CTL: {fatigue['ctl']} | "
        f"Form: {fatigue['form']} → {fatigue['recommendation']}\n\n"
        f"**Today's activity:**\n{_format_todays_strava(today_activities)}\n\n"
        f"**Calendar this week:**\n{_format_calendar_summary(schedule)}\n\n"
        f"**Training plan:**\n{_format_plan_summary(plan)}\n"
        "---"
    )


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

def _get_history_messages() -> list[dict]:
    """Return the last MAX_HISTORY_MESSAGES from conversation.json as API messages."""
    history = load_conversation()
    recent = history[-MAX_HISTORY_MESSAGES:]
    return [
        {"role": msg["role"], "content": msg["content"]}
        for msg in recent
        if msg.get("role") in ("user", "assistant") and msg.get("content")
    ]


def _call_claude(
    user_text: str | None,
    system_override: str | None = None,
    include_history: bool = True,
) -> str:
    """
    Call Claude with full context.

    Args:
        user_text: The user's message, or None if this is a proactive send
                   (e.g. evening check-in initiation).
        system_override: Use a different system prompt (for check-in/missed flows).
        include_history: Whether to prepend conversation history.
    """
    system_prompt = (PROMPTS_DIR / "system.txt").read_text()
    context = build_context_block()
    full_system = f"{system_prompt}\n\n{context}"

    if system_override:
        full_system = system_override

    messages = []

    if include_history:
        messages = _get_history_messages()

    if user_text:
        # Avoid duplicating the last message if it's already in history
        if not messages or messages[-1]["content"] != user_text:
            messages.append({"role": "user", "content": user_text})

    if not messages:
        raise ValueError("No messages to send to Claude.")

    response = client.messages.create(
        model=MODEL,
        max_tokens=400,
        system=full_system,
        messages=messages,
    )

    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Public workflows
# ---------------------------------------------------------------------------

async def handle_message(user_text: str) -> str:
    """
    Main entry point for all incoming Telegram messages.

    Logs the user message, calls Claude with full context, logs the reply,
    and returns the response text.
    """
    # Conversation logging is handled by bot.py before this is called,
    # so we only need to call Claude and log the response.
    reply = _call_claude(user_text)

    append_message(role="assistant", content=reply)
    return reply


async def run_evening_checkin() -> str:
    """
    Build and send the evening check-in message.

    Determines whether the athlete ran today, had a rest day, or missed
    a planned workout, and selects the appropriate flow.
    """
    today_key = datetime.now().strftime("%a").lower()[:3]
    plan = load_plan()
    today_plan = plan.get("days", {}).get(today_key, {}) if plan else {}
    planned_type = today_plan.get("type", "rest")

    today_activities = get_today_activities()
    activity_logged = len(today_activities) > 0

    # Missed workout: planned a run but nothing on Strava
    if planned_type not in ("rest",) and not activity_logged:
        return await run_missed_workout_flow(today_plan)

    # Build check-in prompt
    template = (PROMPTS_DIR / "evening_checkin.txt").read_text()

    todays_activity_str = (
        _format_todays_strava(today_activities) if activity_logged else "none"
    )
    recent_activities = fetch_recent_activities(weeks=6)
    fatigue = calculate_fatigue(recent_activities)

    planned_str = (
        f"{planned_type} {today_plan.get('miles', '')}mi".strip()
        if planned_type != "rest"
        else "rest day"
    )

    checkin_prompt = template.format(
        date=datetime.now().strftime("%A, %B %d"),
        planned_workout=planned_str,
        activity_logged="yes" if activity_logged else "no",
        todays_activity=todays_activity_str,
        form=fatigue["form"],
        recommendation=fatigue["recommendation"],
    )

    reply = _call_claude(
        user_text=None,
        system_override=checkin_prompt,
        include_history=False,
    )

    append_message(role="assistant", content=reply)
    return reply


async def run_missed_workout_flow(planned_workout: dict) -> str:
    """
    Send the initial missed-workout check-in message.
    The athlete's reply is handled by handle_message() which will detect
    context and call adjust_plan() if needed.
    """
    template = (PROMPTS_DIR / "missed_workout.txt").read_text()

    plan = load_plan()
    today_key = datetime.now().strftime("%a").lower()[:3]
    day_order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_idx = day_order.index(today_key) if today_key in day_order else 0

    remaining = {
        k: v
        for k, v in (plan.get("days", {}) if plan else {}).items()
        if day_order.index(k) > today_idx
    }

    recent_activities = fetch_recent_activities(weeks=6)
    fatigue = calculate_fatigue(recent_activities)

    missed_prompt = template.format(
        date=datetime.now().strftime("%A, %B %d"),
        missed_workout=(
            f"{planned_workout.get('type', 'run')} "
            f"{planned_workout.get('miles', '')}mi".strip()
        ),
        remaining_week=json.dumps(remaining, indent=2),
        workout_type=planned_workout.get("type", "run"),
        form=fatigue["form"],
        recommendation=fatigue["recommendation"],
    )

    reply = _call_claude(
        user_text=None,
        system_override=missed_prompt,
        include_history=False,
    )

    append_message(role="assistant", content=reply)
    return reply


async def handle_checkin_reply(user_text: str) -> str:
    """
    Process a reply to an evening check-in: parse wellness data,
    then respond conversationally via handle_message.
    """
    try:
        parse_checkin_reply(user_text)
        logger.info("Check-in reply parsed and logged.")
    except Exception as e:
        logger.warning(f"Could not parse check-in reply: {e}")

    return await handle_message(user_text)


async def handle_missed_workout_reply(user_text: str) -> str:
    """
    Process the athlete's explanation for a missed workout:
    adjust the remaining plan, then send the updated plan.
    """
    try:
        updated_plan = adjust_plan(
            f"Athlete missed today's workout. Their explanation: {user_text}. "
            "Redistribute or drop the missed volume across the remaining days of "
            "the week in a way that keeps total load reasonable."
        )
        plan_text = format_plan_for_telegram(updated_plan)
        reply = (
            f"Got it — adjusted the rest of the week:\n\n{plan_text}"
        )
    except Exception as e:
        logger.error(f"Plan adjustment failed: {e}")
        reply = await handle_message(user_text)

    append_message(role="assistant", content=reply)
    return reply
