"""
tools/fatigue.py — ATL/CTL fatigue model

Spec:
  - Load per run = distance_miles * effort_multiplier
  - ATL = 7-day exponential weighted average of daily load
  - CTL = 42-day exponential weighted average of daily load
  - Form = CTL - ATL

  Form interpretation:
    < -20       → back off significantly
    -20 to -5   → normal training
    -5 to +5    → neutral
    > +5        → can push, freshness is high
"""

from __future__ import annotations

import logging
from datetime import timedelta

from utils import local_now

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EWA decay constants
# ---------------------------------------------------------------------------

ATL_DAYS = 7
CTL_DAYS = 42

ATL_ALPHA = 2 / (ATL_DAYS + 1)  # ≈ 0.25
CTL_ALPHA = 2 / (CTL_DAYS + 1)  # ≈ 0.047


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------

def _build_daily_loads(activities: list[dict], num_days: int) -> list[float]:
    """
    Convert a list of activity dicts (each with 'date' and 'load') into a
    chronological list of daily loads covering the last `num_days` days.
    Days with no activity get load = 0.
    """
    today = local_now().date()
    loads_by_date: dict[str, float] = {}

    for act in activities:
        date_str = act.get("date", "")
        load = act.get("load", 0.0)
        loads_by_date[date_str] = loads_by_date.get(date_str, 0.0) + load

    daily = []
    for i in range(num_days - 1, -1, -1):
        date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append(loads_by_date.get(date_str, 0.0))

    return daily


def _ewma(daily_loads: list[float], alpha: float) -> float:
    """
    Compute exponential weighted moving average over a list of daily loads.
    Seed value is the simple mean of the first window (avoids cold-start bias).
    Returns the final EWA value (today's score).
    """
    if not daily_loads:
        return 0.0

    ewa = daily_loads[0]
    for load in daily_loads[1:]:
        ewa = alpha * load + (1 - alpha) * ewa

    return round(ewa, 2)


def calculate_fatigue(activities: list[dict]) -> dict:
    """
    Given a list of enriched activity dicts (from strava.fetch_recent_activities),
    compute and return ATL, CTL, Form, and a human-readable recommendation.

    activities must cover at least 42 days for CTL to be meaningful; with only
    4 weeks of Strava data the CTL will be slightly underestimated but still useful.

    Returns:
        {
            "atl": float,         # Acute Training Load (7-day EWA)
            "ctl": float,         # Chronic Training Load (42-day EWA)
            "form": float,        # CTL - ATL
            "recommendation": str
        }
    """
    # Build daily load arrays for each window
    atl_loads = _build_daily_loads(activities, ATL_DAYS)
    ctl_loads = _build_daily_loads(activities, CTL_DAYS)

    atl = _ewma(atl_loads, ATL_ALPHA)
    ctl = _ewma(ctl_loads, CTL_ALPHA)
    form = round(ctl - atl, 2)

    recommendation = _interpret_form(form)

    logger.info(f"Fatigue — ATL: {atl}, CTL: {ctl}, Form: {form} → {recommendation}")

    return {
        "atl": atl,
        "ctl": ctl,
        "form": form,
        "recommendation": recommendation,
    }


def _interpret_form(form: float) -> str:
    if form < -20:
        return "back off significantly — accumulated fatigue is high"
    elif form < -5:
        return "normal training load — continue as planned"
    elif form <= 5:
        return "neutral — body is balanced, maintain current intensity"
    else:
        return "can push — freshness is high, good time for a quality session"


# ---------------------------------------------------------------------------
# Convenience wrapper (pulls from Strava cache directly)
# ---------------------------------------------------------------------------

def get_fatigue_scores() -> dict:
    """
    Fetch recent Strava activities and return the current fatigue scores.
    Convenience function for use in agent.py and scheduler jobs.
    """
    from integrations.strava import fetch_recent_activities

    # 42 days for a meaningful CTL window
    activities = fetch_recent_activities(weeks=6)
    return calculate_fatigue(activities)
