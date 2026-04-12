"""
integrations/weather.py — Open-Meteo weather integration for Portland, OR

No API key required. Caches results for 3 hours to avoid redundant calls.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

WEATHER_CACHE_FILE = Path(__file__).parent.parent / "data" / "weather_cache.json"

# Portland, OR coordinates
LAT = 45.5051
LON = -122.6750
TIMEZONE = "America/Los_Angeles"
CACHE_TTL_SECONDS = 10800  # 3 hours

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


# ---------------------------------------------------------------------------
# WMO weather code mapping
# ---------------------------------------------------------------------------

def _categorize_wmo_code(code: int) -> str:
    if code == 0:
        return "clear"
    if code in (1, 2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (51, 53, 55, 61, 63, 80, 81):
        return "rain"
    if code in (65, 82):
        return "heavy_rain"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "thunderstorm"
    return "cloudy"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if WEATHER_CACHE_FILE.exists():
        with open(WEATHER_CACHE_FILE, "r") as f:
            return json.load(f)
    return {}


def _save_cache(data: dict) -> None:
    with open(WEATHER_CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_today_weather() -> dict:
    """
    Fetch today's weather for Portland, OR.
    Returns a dict with:
        {
            "date": "YYYY-MM-DD",
            "temp_max_f": float,
            "temp_min_f": float,
            "precip_mm": float,
            "wind_mph": float,
            "weathercode": int,
            "category": "clear|cloudy|fog|rain|heavy_rain|snow|thunderstorm",
        }
    Returns empty dict on failure (non-fatal).
    """
    cache = _load_cache()
    from utils import local_now
    today = local_now().strftime("%Y-%m-%d")

    # Return from cache if fresh enough
    if cache.get("date") == today and cache.get("fetched_at"):
        age = time.time() - cache["fetched_at"]
        if age < CACHE_TTL_SECONDS:
            logger.info("Returning weather from cache.")
            return cache.get("today", {})

    try:
        resp = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": LAT,
                "longitude": LON,
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,windspeed_10m_max,weathercode"
                ),
                "temperature_unit": "fahrenheit",
                "windspeed_unit": "mph",
                "timezone": TIMEZONE,
                "forecast_days": 7,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Weather fetch failed: {e}")
        return {}

    daily = data.get("daily", {})
    dates = daily.get("time", [])

    if today not in dates:
        logger.warning(f"Today ({today}) not found in weather response.")
        return {}

    idx = dates.index(today)
    code = int(daily["weathercode"][idx])

    today_weather = {
        "date": today,
        "temp_max_f": round(daily["temperature_2m_max"][idx], 1),
        "temp_min_f": round(daily["temperature_2m_min"][idx], 1),
        "precip_mm": round(daily["precipitation_sum"][idx], 1),
        "wind_mph": round(daily["windspeed_10m_max"][idx], 1),
        "weathercode": code,
        "category": _categorize_wmo_code(code),
    }

    # Cache the full 7-day forecast for use by the Sunday planner
    week_forecast = []
    for i, d in enumerate(dates):
        c = int(daily["weathercode"][i])
        week_forecast.append({
            "date": d,
            "temp_max_f": round(daily["temperature_2m_max"][i], 1),
            "precip_mm": round(daily["precipitation_sum"][i], 1),
            "wind_mph": round(daily["windspeed_10m_max"][i], 1),
            "weathercode": c,
            "category": _categorize_wmo_code(c),
        })

    _save_cache({
        "date": today,
        "fetched_at": time.time(),
        "today": today_weather,
        "week": week_forecast,
    })
    logger.info(
        f"Weather fetched: {today_weather['category']} "
        f"{today_weather['temp_max_f']}°F wind {today_weather['wind_mph']}mph"
    )
    return today_weather


def get_week_weather_forecast() -> list[dict]:
    """Return the 7-day forecast, fetching if the cache is stale."""
    fetch_today_weather()  # ensures cache is populated
    return _load_cache().get("week", [])


def get_weather_nudge(weather: dict) -> str:
    """
    Return a one-line weather note appropriate for a running context.
    Returns an empty string for unremarkable conditions.
    """
    if not weather:
        return ""

    temp = weather.get("temp_max_f", 60)
    wind = weather.get("wind_mph", 0)
    precip = weather.get("precip_mm", 0)
    category = weather.get("category", "clear")

    if category == "thunderstorm":
        return f"Thunderstorm — do not run outside today"
    if category == "snow":
        return f"Icy/snowy conditions — treadmill or rest swap recommended"
    if temp >= 90:
        return f"Extreme heat ({temp:.0f}°F) — rest or treadmill only, not worth it"
    if temp >= 75:
        return f"Hot today ({temp:.0f}°F) — go early, extra water, expect slower pace"
    if category == "heavy_rain":
        return f"Heavy rain — trails will be muddy and slippery, consider road route"
    if category == "rain":
        return f"Rainy ({precip:.1f}mm) — wet out there, trails may be muddy"
    if wind >= 25:
        return f"Very windy ({wind:.0f}mph) — expect slower paces, don't chase numbers"
    if wind >= 20:
        return f"Windy ({wind:.0f}mph) — adjust effort expectations"
    if category == "fog":
        return f"Foggy — stick to familiar trails, watch footing"
    if temp <= 32:
        return f"Freezing ({temp:.0f}°F) — roads may be icy, treadmill is the safer call"

    # Perfect conditions
    if 45 <= temp <= 65 and category in ("clear", "cloudy") and wind < 15:
        return f"Great running weather ({temp:.0f}°F, {category})"

    return ""


def is_dangerous_weather(weather: dict) -> bool:
    """True if conditions warrant a proactive workout-swap recommendation."""
    if not weather:
        return False
    return (
        weather.get("category") in ("snow", "thunderstorm")
        or weather.get("temp_max_f", 60) >= 90
    )


def format_weather_for_context(weather: dict) -> str:
    """One-line weather summary for the agent context block."""
    if not weather:
        return "  Weather unavailable."
    nudge = get_weather_nudge(weather)
    if nudge:
        return f"  {nudge}"
    temp = weather.get("temp_max_f", "?")
    cat = weather.get("category", "")
    return f"  {temp}°F, {cat}"
