"""
agent.py — Claude brain for Ultra Coach

Assembles full context and handles all message types:
  - Freeform messages (anytime), including race registration
  - Evening check-in initiation
  - Missed workout detection and flow
  - Plan adjustment after athlete reply
  - Post-activity "how did that feel?" flow
  - Post-race result collection
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from utils import local_now

import anthropic
from dotenv import load_dotenv

from bot import append_message, load_conversation
from integrations.strava import fetch_recent_activities, get_today_activities
from integrations.calendar import fetch_week_schedule
from integrations.weather import fetch_today_weather, format_weather_for_context
from tools.fatigue import calculate_fatigue
from tools.parser import parse_checkin_reply, get_todays_log
from tools.planner import load_plan, adjust_plan, format_plan_for_telegram
from integrations.health import get_todays_health, get_recent_health
from tools.memory import (
    format_profile_for_context,
    format_recent_memos_for_context,
    extract_and_update_facts,
)
from tools.races import (
    format_races_for_context,
    format_phase_for_context,
    get_phase_context,
    compute_vert_target,
    _looks_like_race_message,
    parse_race_intent,
    add_race,
    update_race,
    remove_race,
    log_race_result,
)
from state import get_context, FLOW_POST_ACTIVITY_REPLY

load_dotenv()

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
MODEL = "claude-opus-4-5"
MAX_HISTORY_MESSAGES = 40

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _format_plan_summary(plan: dict | None) -> str:
    if not plan:
        return "No plan for this week yet."

    today_key = local_now().strftime("%a").lower()[:3]
    days = plan.get("days", {})
    target_elev = plan.get("target_elevation_ft", "?")
    elev_str = f"{int(target_elev):,}ft" if isinstance(target_elev, (int, float)) else str(target_elev)
    lines = [f"Week of {plan.get('week_of')} — {plan.get('target_miles')}mi / {elev_str}"]

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


def _format_health(today: dict | None, recent: list[dict]) -> str:
    if not today and not recent:
        return "  No health data logged yet."
    lines = []
    if today:
        weight = today.get("weight_lbs")
        sleep = today.get("sleep_hours")
        parts = []
        if weight:
            parts.append(f"{weight}lbs")
        if sleep:
            parts.append(f"{sleep}hrs sleep")
        lines.append(f"  Today: {' | '.join(parts)}" if parts else "  Today: no data")
    if recent:
        weights = [e["weight_lbs"] for e in recent if e.get("weight_lbs")]
        sleeps = [e["sleep_hours"] for e in recent if e.get("sleep_hours")]
        if weights:
            lines.append(f"  7-day avg weight: {sum(weights)/len(weights):.1f}lbs")
        if sleeps:
            lines.append(f"  7-day avg sleep: {sum(sleeps)/len(sleeps):.1f}hrs")
    return "\n".join(lines) if lines else "  No health data logged yet."


def _format_recent_activities(activities: list[dict]) -> str:
    if not activities:
        return "  No recent runs found."
    lines = []
    for act in activities[-20:]:
        sport = act.get("effort", "")
        lines.append(
            f"  {act['date']} — {act['distance_miles']:.1f}mi | {sport}"
            + (f" | {act.get('elevation_gain_meters', 0):.0f}m gain" if act.get('elevation_gain_meters') else "")
            + (f" | HR {act['average_heartrate']:.0f}" if act.get("average_heartrate") else "")
        )
    total_miles = sum(a["distance_miles"] for a in activities)
    total_vert_m = sum(a.get("elevation_gain_meters", 0) for a in activities)
    total_vert_ft = int(total_vert_m * 3.28084)
    lines.append(f"  Total ({len(activities)} runs): {total_miles:.1f}mi | {total_vert_ft:,}ft gain")
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
    today_str = local_now().strftime("%Y-%m-%d")
    for date_str, tag in sorted(schedule.items()):
        day_name = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a")
        marker = " ← today" if date_str == today_str else ""
        lines.append(f"  {day_name} {date_str}: {tag}{marker}")
    return "\n".join(lines)


def _compute_weekly_vert_actual(activities: list[dict]) -> int:
    """Sum elevation gain in feet for the current calendar week (Mon–Sun)."""
    today = local_now().date()
    monday = today - timedelta(days=today.weekday())
    week_start = monday.strftime("%Y-%m-%d")
    total_meters = sum(
        a.get("elevation_gain_meters", 0)
        for a in activities
        if a.get("date", "") >= week_start
    )
    return int(total_meters * 3.28084)


def build_context_block() -> str:
    """
    Assemble a full dynamic context block injected into the system prompt.
    Pulls live data from Strava, Calendar, Weather, Fatigue model, Race/Phase, and the current plan.
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

    try:
        weather = fetch_today_weather()
    except Exception as e:
        logger.warning(f"Weather fetch failed: {e}")
        weather = {}

    fatigue = calculate_fatigue(recent_activities)
    plan = load_plan()
    todays_health = get_todays_health()
    recent_health = get_recent_health(days=7)

    # Vert tracking
    weekly_vert_actual = _compute_weekly_vert_actual(recent_activities)
    phase_ctx = get_phase_context()
    vert_target = compute_vert_target(phase_ctx)
    vert_str = f"  {weekly_vert_actual:,}ft this week / {vert_target:,}ft target ({phase_ctx['phase']} phase)"

    return (
        "---\n"
        "## Live Context (updated each message)\n\n"
        f"**Date:** {local_now().strftime('%A, %B %d %Y')}\n\n"
        f"**Athlete profile:**\n{format_profile_for_context()}\n\n"
        f"**Recent weekly memos:**\n{format_recent_memos_for_context()}\n\n"
        f"**Upcoming races:**\n{format_races_for_context()}\n\n"
        f"**Training phase:**\n{format_phase_for_context()}\n\n"
        f"**Fatigue:**\n"
        f"  ATL: {fatigue['atl']} | CTL: {fatigue['ctl']} | "
        f"Form: {fatigue['form']} → {fatigue['recommendation']}\n\n"
        f"**Today's health data:**\n{_format_health(todays_health, recent_health)}\n\n"
        f"**Today's activity:**\n{_format_todays_strava(today_activities)}\n\n"
        f"**Today's weather:**\n{format_weather_for_context(weather)}\n\n"
        f"**Weekly vert:**\n{vert_str}\n\n"
        f"**Recent runs (last 6 weeks):**\n{_format_recent_activities(recent_activities)}\n\n"
        f"**Calendar this week:**\n{_format_calendar_summary(schedule)}\n\n"
        f"**Training plan:**\n{_format_plan_summary(plan)}\n"
        "---"
    )


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

def _get_history_messages() -> list[dict]:
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
    system_prompt = (PROMPTS_DIR / "system.txt").read_text()
    context = build_context_block()
    full_system = f"{system_prompt}\n\n{context}"

    if system_override:
        full_system = system_override

    messages = []

    if include_history:
        messages = _get_history_messages()

    if user_text:
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
# Race intent handling
# ---------------------------------------------------------------------------

def _try_handle_race_intent(user_text: str) -> None:
    """
    Silently parse and apply any race management intent in the message.
    Does nothing if the message is not race-related.
    The actual conversational response comes from the main _call_claude() call,
    which will see the updated race context.
    """
    if not _looks_like_race_message(user_text):
        return

    intent_data = parse_race_intent(user_text)
    if not intent_data:
        return

    intent = intent_data.get("intent")
    try:
        if intent == "add":
            race = {k: v for k, v in intent_data.items() if k not in ("intent",) and v is not None}
            add_race(race)
            logger.info(f"Race added via message: {race.get('name')}")
        elif intent == "update":
            update_race(intent_data.get("name", ""), intent_data.get("updates", {}))
        elif intent == "remove":
            remove_race(intent_data.get("name", ""))
    except Exception as e:
        logger.warning(f"Race intent handling failed: {e}")


# ---------------------------------------------------------------------------
# Background fact extraction
# ---------------------------------------------------------------------------

async def _extract_facts_bg(user_text: str, assistant_text: str) -> None:
    """Fire-and-forget: extract memorable facts from one exchange and save to profile."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, extract_and_update_facts, user_text, assistant_text)


# ---------------------------------------------------------------------------
# Public workflows
# ---------------------------------------------------------------------------

async def handle_message(user_text: str) -> str:
    """
    Main entry point for all incoming Telegram messages.

    Detects race management intent (silently saves data if found),
    then calls Claude with full context and returns the response.
    """
    _try_handle_race_intent(user_text)

    reply = _call_claude(user_text)
    append_message(role="assistant", content=reply)
    asyncio.create_task(_extract_facts_bg(user_text, reply))
    return reply


async def run_evening_checkin() -> str:
    """
    Build and send the evening check-in message.
    """
    today_key = local_now().strftime("%a").lower()[:3]
    plan = load_plan()
    today_plan = plan.get("days", {}).get(today_key, {}) if plan else {}
    planned_type = today_plan.get("type", "rest")

    today_activities = get_today_activities()
    activity_logged = len(today_activities) > 0

    if planned_type not in ("rest",) and not activity_logged:
        return await run_missed_workout_flow(today_plan)

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
        date=local_now().strftime("%A, %B %d"),
        planned_workout=planned_str,
        activity_logged="yes" if activity_logged else "no",
        todays_activity=todays_activity_str,
        form=fatigue["form"],
        recommendation=fatigue["recommendation"],
    )

    reply = _call_claude(
        user_text=None,
        system_override=checkin_prompt,
    )

    append_message(role="assistant", content=reply)
    return reply


async def run_missed_workout_flow(planned_workout: dict) -> str:
    """
    Send the initial missed-workout check-in message.
    """
    template = (PROMPTS_DIR / "missed_workout.txt").read_text()

    plan = load_plan()
    today_key = local_now().strftime("%a").lower()[:3]
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
        date=local_now().strftime("%A, %B %d"),
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
    )

    append_message(role="assistant", content=reply)
    return reply


async def handle_checkin_reply(user_text: str) -> str:
    """
    Process a reply to an evening check-in: parse wellness data,
    then respond conversationally.
    """
    try:
        parse_checkin_reply(user_text)
        logger.info("Check-in reply parsed and logged.")
    except Exception as e:
        logger.warning(f"Could not parse check-in reply: {e}")

    return await handle_message(user_text)


async def handle_missed_workout_reply(user_text: str) -> str:
    """
    Process the athlete's explanation for a missed workout and adjust the plan.
    """
    try:
        updated_plan = adjust_plan(
            f"Athlete missed today's workout. Their explanation: {user_text}. "
            "Redistribute or drop the missed volume across the remaining days of "
            "the week in a way that keeps total load reasonable."
        )
        plan_text = format_plan_for_telegram(updated_plan)
        reply = f"Got it — adjusted the rest of the week:\n\n{plan_text}"
    except Exception as e:
        logger.error(f"Plan adjustment failed: {e}")
        reply = await handle_message(user_text)

    append_message(role="assistant", content=reply)
    return reply


async def run_post_activity_checkin(activity: dict) -> str:
    """
    Send a short "how did that feel?" message after a new Strava activity is detected.
    References the prescribed workout type if a plan exists.
    """
    distance = activity.get("distance_miles", 0)
    effort = activity.get("effort", "")
    name = activity.get("name", "that run")

    # Pull prescribed workout type for context
    today_key = local_now().strftime("%a").lower()[:3]
    plan = load_plan()
    today_plan = plan.get("days", {}).get(today_key, {}) if plan else {}
    prescribed_type = today_plan.get("type", "")
    prescribed_note = today_plan.get("notes", "")

    prescription_context = ""
    if prescribed_type and prescribed_type not in ("rest",):
        prescription_context = (
            f" It was prescribed as a {prescribed_type}"
            + (f" ({prescribed_note})" if prescribed_note else "")
            + "."
        )

    prompt = (
        f"You are a running coach. Your athlete just logged a {distance:.1f}-mile "
        f"{effort} run on Strava ({name}).{prescription_context} "
        "Send a single short message (under 30 words) asking how it felt. "
        "Be specific to the run — reference the distance, effort type, or prescription. "
        "No bullet points. Just the message text."
    )

    reply = _call_claude(user_text=None, system_override=prompt, include_history=True)
    append_message(role="assistant", content=reply)
    return reply


async def handle_post_activity_reply(user_text: str) -> str:
    """
    Process the athlete's response to a post-activity check-in.
    Adjusts the remaining week if they report significant fatigue or pain.
    """
    activity = get_context().get("activity", {})
    activity_desc = (
        f"{activity.get('distance_miles', '?')}mi {activity.get('effort', '')} run"
        if activity else "the run"
    )

    plan = load_plan()
    today_key = local_now().strftime("%a").lower()[:3]
    day_order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_idx = day_order.index(today_key) if today_key in day_order else 0
    remaining = {
        k: v for k, v in (plan.get("days", {}) if plan else {}).items()
        if day_order.index(k) > today_idx
    }

    assessment_prompt = (
        "You are a running coach. Your athlete just completed a run and you asked how it felt. "
        f"The run: {activity_desc}. "
        f"Their response: \"{user_text}\"\n\n"
        f"Remaining week plan: {json.dumps(remaining)}\n\n"
        "Respond in under 60 words. Acknowledge what they said. "
        "If they felt great or fine, affirm it and move on. "
        "If they felt significantly fatigued, beat up, or mention pain — "
        "briefly note you'll ease up the next session and state which day gets adjusted and how. "
        "Never lecture. Be the calmest person in the conversation."
    )

    reply = _call_claude(user_text=None, system_override=assessment_prompt, include_history=True)

    negative_signals = ["ease", "back off", "reduce", "cut", "adjust", "drop", "lighter"]
    if any(s in reply.lower() for s in negative_signals):
        try:
            adjust_plan(
                f"Athlete reported feeling: \"{user_text}\" after today's {activity_desc}. "
                "Ease the next hard session slightly — reduce intensity or mileage by ~15%, "
                "keep rest days as-is."
            )
            logger.info("Plan adjusted based on post-activity feedback.")
        except Exception as e:
            logger.warning(f"Post-activity plan adjustment failed: {e}")

    append_message(role="assistant", content=reply)
    asyncio.create_task(_extract_facts_bg(user_text, reply))
    return reply


async def run_post_race_checkin(race: dict) -> str:
    """
    Send a warm "how did the race go?" message after a race date passes.
    """
    name = race.get("name", "the race")
    date_str = race.get("date", "")
    dist = race.get("distance_miles", "")
    dist_str = f"{dist}mi " if dist else ""

    prompt = (
        f"You are a running coach. Your athlete just ran the {name} ({dist_str}race) on {date_str}. "
        "Send ONE short message (under 40 words) asking how it went — "
        "time, how they felt, anything to note. Warm and curious, not clinical."
    )

    reply = _call_claude(user_text=None, system_override=prompt, include_history=True)
    append_message(role="assistant", content=reply)
    return reply


async def handle_race_result_reply(user_text: str) -> str:
    """
    Process the athlete's post-race report. Logs the result and transitions
    the conversation naturally toward recovery and next phase.
    """
    race_name = get_context().get("race_name", "")

    # Log the result
    if race_name:
        try:
            log_race_result(race_name, {"notes": user_text})
            logger.info(f"Race result logged for {race_name}")
        except Exception as e:
            logger.warning(f"Could not log race result: {e}")

    # Claude responds naturally — context block now shows post_race phase
    reply = _call_claude(user_text)
    append_message(role="assistant", content=reply)
    asyncio.create_task(_extract_facts_bg(user_text, reply))
    return reply


async def handle_image_message(image_bytes: bytes, mime_type: str, caption: str | None) -> str:
    """
    Process an image sent by the athlete (e.g. a Coros sleep screenshot).
    Sends the image to Claude with full coaching context so it can interpret
    the data and respond as a coach.
    """
    system_prompt = (PROMPTS_DIR / "system.txt").read_text()
    context = build_context_block()
    full_system = f"{system_prompt}\n\n{context}"

    # Build the user content block: image + optional caption / default prompt
    user_content_text = caption if caption else (
        "I sent you a screenshot — what does it show and what does it mean for my training?"
    )

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    messages = _get_history_messages()
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": image_b64,
                },
            },
            {"type": "text", "text": user_content_text},
        ],
    })

    response = client.messages.create(
        model=MODEL,
        max_tokens=400,
        system=full_system,
        messages=messages,
    )
    reply = response.content[0].text.strip()

    # Log as text so future history makes sense without re-sending the image
    log_text = f"[Image: {caption}]" if caption else "[Image sent]"
    append_message(role="user", content=log_text)
    append_message(role="assistant", content=reply)
    asyncio.create_task(_extract_facts_bg(log_text, reply))
    return reply
