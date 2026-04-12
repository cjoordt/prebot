"""
scheduler.py — APScheduler cron jobs for Ultra Coach

Jobs:
  1. Sunday 7pm Pacific   — Generate weekly training plan
  2. Daily 9pm Pacific    — Evening check-in
  3. Daily 11am Pacific   — Morning Strava check
  4. Daily 7pm Pacific    — Evening Strava check (triggers missed workout if needed)
"""

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

TIMEZONE = "America/Los_Angeles"


# ---------------------------------------------------------------------------
# Job implementations
# ---------------------------------------------------------------------------

async def job_weekly_plan() -> None:
    """Sunday 7pm — Write weekly memo, update profile if due, generate and send the next week's plan."""
    logger.info("Running job: weekly plan generation")
    try:
        from integrations.strava import fetch_recent_activities
        from integrations.calendar import fetch_week_schedule
        from integrations.health import get_recent_health
        from tools.fatigue import calculate_fatigue
        from tools.planner import generate_weekly_plan, format_plan_for_telegram, load_plan
        from tools.parser import load_log
        from tools.memory import (
            generate_weekly_memo, update_athlete_profile,
            load_memos, format_recent_memos_for_context,
        )
        from bot import send_message

        activities = fetch_recent_activities(weeks=6, force_refresh=True)
        fatigue = calculate_fatigue(activities)
        schedule = fetch_week_schedule(days=7)
        plan = load_plan()
        activity_log = load_log()
        health_log = get_recent_health(days=7)

        # --- Write this week's coaching memo ---
        from datetime import date, timedelta
        last_monday = (date.today() - timedelta(days=date.today().weekday())).strftime("%Y-%m-%d")
        try:
            generate_weekly_memo(
                week_of=last_monday,
                activities=[a for a in activities if a["date"] >= last_monday],
                activity_log=[e for e in activity_log if e.get("date", "") >= last_monday],
                health_log=health_log,
                plan=plan,
                fatigue=fatigue,
            )
            logger.info("Weekly memo written.")
        except Exception as e:
            logger.warning(f"Memo generation failed (non-fatal): {e}")

        # --- Update athlete profile every 4 weeks (when there are enough memos) ---
        all_memos = load_memos()
        if len(all_memos) > 0 and len(all_memos) % 4 == 0:
            try:
                update_athlete_profile(activities, activity_log, health_log, all_memos)
                logger.info("Athlete profile updated.")
            except Exception as e:
                logger.warning(f"Profile update failed (non-fatal): {e}")

        # --- Generate next week's plan ---
        new_plan = generate_weekly_plan(fatigue, schedule, activities)
        text = format_plan_for_telegram(new_plan)

        await send_message(text)
        logger.info("Weekly plan sent.")
    except Exception as e:
        logger.exception(f"Weekly plan job failed: {e}")


async def job_evening_checkin() -> None:
    """Daily 9pm — Send the appropriate evening check-in message."""
    logger.info("Running job: evening check-in")
    try:
        from agent import run_evening_checkin
        from bot import send_message
        from state import set_flow, FLOW_CHECKIN_REPLY

        message = await run_evening_checkin()
        await send_message(message)
        set_flow(FLOW_CHECKIN_REPLY)
    except Exception as e:
        logger.exception(f"Evening check-in job failed: {e}")


async def job_strava_check(trigger_missed_if_no_activity: bool = False) -> None:
    """
    Check Strava for new activities and compare to today's plan.

    Args:
        trigger_missed_if_no_activity: If True (7pm run), trigger missed
            workout flow when a run was planned but nothing is logged.
    """
    logger.info(f"Running job: Strava check (trigger_missed={trigger_missed_if_no_activity})")
    try:
        from integrations.strava import get_today_activities, fetch_recent_activities
        from tools.planner import load_plan
        from agent import run_missed_workout_flow
        from bot import send_message, append_message

        today_activities = get_today_activities()
        today_key = datetime.now().strftime("%a").lower()[:3]
        plan = load_plan()
        today_plan = plan.get("days", {}).get(today_key, {}) if plan else {}
        planned_type = today_plan.get("type", "rest")

        if today_activities:
            act = today_activities[0]
            logger.info(
                f"Activity found: {act['distance_miles']:.1f}mi | "
                f"{act['effort']} | load {act['load']}"
            )

            # Note significant deviations from plan (>20% off target miles)
            planned_miles = today_plan.get("miles")
            if planned_miles and planned_type not in ("rest",):
                deviation = abs(act["distance_miles"] - planned_miles) / planned_miles
                if deviation > 0.20:
                    note = (
                        f"Logged {act['distance_miles']:.1f}mi vs planned {planned_miles}mi — "
                        f"noted, will factor in."
                    )
                    await send_message(note)
                    append_message(role="assistant", content=note)

        elif trigger_missed_if_no_activity and planned_type not in ("rest",):
            logger.info("No activity logged by 7pm — triggering missed workout flow.")
            from state import set_flow, FLOW_MISSED_WORKOUT_REPLY
            message = await run_missed_workout_flow(today_plan)
            await send_message(message)
            set_flow(FLOW_MISSED_WORKOUT_REPLY)

    except Exception as e:
        logger.exception(f"Strava check job failed: {e}")


async def job_morning_strava_check() -> None:
    """Daily 11am — Check for morning runs, no missed-workout trigger."""
    await job_strava_check(trigger_missed_if_no_activity=False)


async def job_evening_strava_check() -> None:
    """Daily 7pm — Check Strava; trigger missed workout flow if nothing logged."""
    await job_strava_check(trigger_missed_if_no_activity=True)


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def create_scheduler() -> AsyncIOScheduler:
    """Build and return a configured AsyncIOScheduler (not yet started)."""
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # 1. Sunday 7pm — Weekly plan
    scheduler.add_job(
        job_weekly_plan,
        CronTrigger(day_of_week="sun", hour=19, minute=0, timezone=TIMEZONE),
        id="weekly_plan",
        name="Sunday Weekly Plan",
        misfire_grace_time=300,
    )

    # 2. Daily 9pm — Evening check-in
    scheduler.add_job(
        job_evening_checkin,
        CronTrigger(hour=21, minute=0, timezone=TIMEZONE),
        id="evening_checkin",
        name="Evening Check-In",
        misfire_grace_time=300,
    )

    # 3. Daily 11am — Morning Strava check
    scheduler.add_job(
        job_morning_strava_check,
        CronTrigger(hour=11, minute=0, timezone=TIMEZONE),
        id="strava_morning",
        name="Morning Strava Check",
        misfire_grace_time=300,
    )

    # 4. Daily 7pm — Evening Strava check
    scheduler.add_job(
        job_evening_strava_check,
        CronTrigger(hour=19, minute=0, timezone=TIMEZONE),
        id="strava_evening",
        name="Evening Strava Check",
        misfire_grace_time=300,
    )

    logger.info("Scheduler configured: 4 jobs registered.")
    return scheduler
