"""
tests/test_race_parser.py

Tests for race registration parsing, CRUD operations, and the keyword heuristic.
Claude calls are mocked so no API key is required.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.races import (
    _looks_like_race_message,
    parse_race_intent,
    add_race,
    update_race,
    remove_race,
    get_upcoming_races,
    get_active_race,
    load_races,
    save_races,
    RACES_FILE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_claude(json_str: str):
    """Build a mock Anthropic response that returns the given JSON string."""
    mock_content = MagicMock()
    mock_content.text = json_str
    mock_response = MagicMock()
    mock_response.content = [mock_content]
    return mock_response


def _use_temp_races_file(tmp_path):
    """Patch RACES_FILE to use a temp file, return the temp path."""
    races_path = tmp_path / "races.json"
    races_path.write_text('{"races": []}')
    return races_path


# ---------------------------------------------------------------------------
# Keyword heuristic
# ---------------------------------------------------------------------------

class TestLooksLikeRaceMessage:
    def test_signed_up(self):
        assert _looks_like_race_message("I signed up for a 50k") is True

    def test_registered(self):
        assert _looks_like_race_message("Just registered for a race") is True

    def test_50k_mention(self):
        assert _looks_like_race_message("Wyeast Wonder 50k looks brutal") is True

    def test_drop_the(self):
        assert _looks_like_race_message("I need to drop the race") is True

    def test_not_doing(self):
        assert _looks_like_race_message("Not doing the marathon anymore") is True

    def test_unrelated_message(self):
        assert _looks_like_race_message("How did my run feel today?") is False

    def test_check_in_message(self):
        assert _looks_like_race_message("Slept 7 hours, legs feel ok") is False

    def test_case_insensitive(self):
        assert _looks_like_race_message("I SIGNED UP FOR A 50K") is True

    def test_upcoming_race_query(self):
        assert _looks_like_race_message("What upcoming races do I have?") is True


# ---------------------------------------------------------------------------
# parse_race_intent (Claude mocked)
# ---------------------------------------------------------------------------

class TestParseRaceIntent:
    def test_add_race_full_details(self):
        payload = {
            "intent": "add",
            "name": "Wyeast Wonder 50k",
            "date": "2026-09-12",
            "distance_miles": 31.1,
            "elevation_gain_ft": 7000,
            "course_notes": "Technical trail",
            "goal": "finish",
            "priority": None,
        }
        with patch("tools.races.client.messages.create") as mock_create:
            mock_create.return_value = _mock_claude(json.dumps(payload))
            result = parse_race_intent("I signed up for Wyeast Wonder 50k on September 12")

        assert result is not None
        assert result["intent"] == "add"
        assert result["name"] == "Wyeast Wonder 50k"
        assert result["date"] == "2026-09-12"
        assert result["distance_miles"] == 31.1
        assert result["elevation_gain_ft"] == 7000

    def test_add_race_minimal(self):
        payload = {
            "intent": "add",
            "name": "Portland Trail 25k",
            "date": "2026-11-08",
            "distance_miles": 15.5,
            "elevation_gain_ft": None,
            "course_notes": None,
            "goal": None,
            "priority": None,
        }
        with patch("tools.races.client.messages.create") as mock_create:
            mock_create.return_value = _mock_claude(json.dumps(payload))
            result = parse_race_intent("New race: Portland Trail 25k, November 8")

        assert result["intent"] == "add"
        assert result["distance_miles"] == 15.5

    def test_remove_race(self):
        payload = {"intent": "remove", "name": "Wyeast Wonder"}
        with patch("tools.races.client.messages.create") as mock_create:
            mock_create.return_value = _mock_claude(json.dumps(payload))
            result = parse_race_intent("Drop the Wyeast Wonder")

        assert result["intent"] == "remove"
        assert "wyeast" in result["name"].lower()

    def test_update_race_date(self):
        payload = {
            "intent": "update",
            "name": "Wyeast Wonder",
            "updates": {"date": "2026-09-19"},
        }
        with patch("tools.races.client.messages.create") as mock_create:
            mock_create.return_value = _mock_claude(json.dumps(payload))
            result = parse_race_intent("Wyeast is actually on September 19")

        assert result["intent"] == "update"
        assert result["updates"]["date"] == "2026-09-19"

    def test_non_race_message_returns_none(self):
        payload = {"intent": "none"}
        with patch("tools.races.client.messages.create") as mock_create:
            mock_create.return_value = _mock_claude(json.dumps(payload))
            result = parse_race_intent("How are my legs feeling?")

        assert result is None

    def test_api_failure_returns_none(self):
        with patch("tools.races.client.messages.create") as mock_create:
            mock_create.side_effect = Exception("API error")
            result = parse_race_intent("I signed up for a race")

        assert result is None

    def test_malformed_json_returns_none(self):
        with patch("tools.races.client.messages.create") as mock_create:
            mock_create.return_value = _mock_claude("not valid json {{")
            result = parse_race_intent("I signed up for something")

        assert result is None


# ---------------------------------------------------------------------------
# CRUD operations (using patched RACES_FILE)
# ---------------------------------------------------------------------------

class TestRaceCRUD:
    @pytest.fixture(autouse=True)
    def patch_races_file(self, tmp_path):
        """Redirect all CRUD operations to a temp file for test isolation."""
        temp_file = tmp_path / "races.json"
        temp_file.write_text('{"races": []}')
        with patch("tools.races.RACES_FILE", temp_file):
            yield temp_file

    def test_add_race_saves_to_file(self):
        race = {
            "name": "Wyeast Wonder 50k",
            "date": "2026-09-12",
            "distance_miles": 31.1,
        }
        add_race(race)
        races = load_races()
        assert len(races) == 1
        assert races[0]["name"] == "Wyeast Wonder 50k"

    def test_add_race_assigns_priority_a_when_empty(self):
        race = {"name": "Test Race", "date": "2026-09-01", "distance_miles": 31.0}
        saved = add_race(race)
        assert saved["priority"] == "A"

    def test_add_second_race_gets_priority_b(self):
        add_race({"name": "Race A", "date": "2026-09-01", "distance_miles": 31.0, "priority": "A"})
        saved = add_race({"name": "Race B", "date": "2026-11-01", "distance_miles": 15.0})
        assert saved["priority"] == "B"

    def test_add_race_replaces_same_name(self):
        add_race({"name": "Wyeast Wonder 50k", "date": "2026-09-12", "distance_miles": 31.1})
        add_race({"name": "Wyeast Wonder 50k", "date": "2026-09-19", "distance_miles": 31.1})
        races = load_races()
        assert len(races) == 1
        assert races[0]["date"] == "2026-09-19"

    def test_add_race_case_insensitive_dedup(self):
        add_race({"name": "wyeast wonder 50k", "date": "2026-09-12", "distance_miles": 31.1})
        add_race({"name": "Wyeast Wonder 50k", "date": "2026-09-19", "distance_miles": 31.1})
        races = load_races()
        assert len(races) == 1

    def test_update_race_found(self):
        add_race({"name": "Wyeast Wonder 50k", "date": "2026-09-12", "distance_miles": 31.1})
        result = update_race("Wyeast", {"date": "2026-09-19"})
        assert result is True
        races = load_races()
        assert races[0]["date"] == "2026-09-19"

    def test_update_race_not_found(self):
        result = update_race("Nonexistent Race", {"date": "2026-01-01"})
        assert result is False

    def test_remove_race_found(self):
        add_race({"name": "Wyeast Wonder 50k", "date": "2026-09-12", "distance_miles": 31.1})
        result = remove_race("wyeast")
        assert result is True
        races = load_races()
        assert len(races) == 0

    def test_remove_race_not_found(self):
        result = remove_race("nonexistent")
        assert result is False

    def test_get_upcoming_races_filters_past(self):
        from datetime import datetime, timedelta
        past = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

        save_races([
            {"name": "Past Race", "date": past, "distance_miles": 31.0},
            {"name": "Future Race", "date": future, "distance_miles": 31.0},
        ])
        upcoming = get_upcoming_races()
        assert len(upcoming) == 1
        assert upcoming[0]["name"] == "Future Race"

    def test_get_upcoming_races_sorted_by_date(self):
        save_races([
            {"name": "Later Race", "date": "2026-12-01", "distance_miles": 31.0},
            {"name": "Sooner Race", "date": "2026-09-01", "distance_miles": 31.0},
        ])
        upcoming = get_upcoming_races()
        assert upcoming[0]["name"] == "Sooner Race"

    def test_get_active_race_returns_a_priority(self):
        from datetime import datetime, timedelta
        d1 = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
        d2 = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        save_races([
            {"name": "B Race", "date": d1, "distance_miles": 15.0, "priority": "B"},
            {"name": "A Race", "date": d2, "distance_miles": 31.0, "priority": "A"},
        ])
        active = get_active_race()
        assert active["name"] == "A Race"

    def test_get_active_race_no_races_returns_none(self):
        active = get_active_race()
        assert active is None
