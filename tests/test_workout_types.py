"""
tests/test_workout_types.py

Tests for workout type validation logic:
- 80/20 intensity rule
- No back-to-back hard days
- Phase-appropriate workout types
"""

from __future__ import annotations

import pytest

from tools.planner import validate_plan, HARD_TYPES, PHASE_FORBIDDEN_TYPES


def _make_plan(day_specs: dict) -> dict:
    """
    Build a minimal plan dict from a day_key → (type, miles) mapping.
    Unspecified days default to rest.
    """
    days = {}
    day_order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    for key in day_order:
        if key in day_specs:
            dtype, miles = day_specs[key]
            days[key] = {"type": dtype, "miles": miles}
        else:
            days[key] = {"type": "rest"}
    return {"week_of": "2026-04-13", "days": days}


# ---------------------------------------------------------------------------
# Back-to-back hard day checks
# ---------------------------------------------------------------------------

class TestBackToBackHardDays:
    def test_single_hard_day_no_violation(self):
        plan = _make_plan({
            "mon": ("easy", 5),
            "wed": ("tempo", 6),
            "fri": ("easy", 5),
            "sat": ("long", 10),
        })
        warnings = validate_plan(plan, "strength")
        assert not any("back-to-back" in w.lower() for w in warnings)

    def test_two_consecutive_hard_days_flagged(self):
        plan = _make_plan({
            "mon": ("tempo", 6),
            "tue": ("intervals", 7),
            "sat": ("long", 10),
        })
        warnings = validate_plan(plan, "strength")
        assert any("back-to-back" in w.lower() for w in warnings)

    def test_hard_day_followed_by_easy_is_ok(self):
        plan = _make_plan({
            "mon": ("tempo", 6),
            "tue": ("easy", 5),
            "wed": ("intervals", 6),
        })
        warnings = validate_plan(plan, "strength")
        assert not any("back-to-back" in w.lower() for w in warnings)

    def test_saturday_sunday_long_runs_not_hard(self):
        # long is not in HARD_TYPES — back-to-back long is fine and desired
        plan = _make_plan({
            "sat": ("long", 14),
            "sun": ("long", 10),
        })
        warnings = validate_plan(plan, "race_specific")
        assert not any("back-to-back" in w.lower() for w in warnings)

    def test_three_consecutive_hard_days_flagged_twice(self):
        plan = _make_plan({
            "mon": ("tempo", 6),
            "tue": ("intervals", 7),
            "wed": ("hill_repeats", 5),
        })
        warnings = validate_plan(plan, "strength")
        back_to_back = [w for w in warnings if "back-to-back" in w.lower()]
        assert len(back_to_back) >= 2


# ---------------------------------------------------------------------------
# 80/20 intensity rule
# ---------------------------------------------------------------------------

class TestEightyTwentyRule:
    def test_valid_80_20_split(self):
        # 5mi hard out of 30mi total = 16.7% — fine
        plan = _make_plan({
            "mon": ("easy", 5),
            "tue": ("easy", 6),
            "wed": ("tempo", 5),
            "thu": ("easy", 4),
            "fri": ("easy", 5),
            "sat": ("long", 5),
        })
        warnings = validate_plan(plan, "strength")
        assert not any("20%" in w for w in warnings)

    def test_too_much_hard_work_flagged(self):
        # 15mi hard out of 25mi total = 60% — violation
        plan = _make_plan({
            "mon": ("easy", 5),
            "tue": ("tempo", 8),
            "thu": ("intervals", 7),
        })
        warnings = validate_plan(plan, "strength")
        assert any("20%" in w for w in warnings)

    def test_all_easy_is_fine(self):
        plan = _make_plan({
            "mon": ("easy", 5),
            "wed": ("easy", 6),
            "fri": ("easy", 5),
            "sat": ("long", 10),
        })
        warnings = validate_plan(plan, "base")
        assert not any("20%" in w for w in warnings)

    def test_all_rest_no_division_error(self):
        plan = _make_plan({})
        warnings = validate_plan(plan, "general")
        # Should not raise any exceptions
        assert isinstance(warnings, list)


# ---------------------------------------------------------------------------
# Phase-appropriate workout types
# ---------------------------------------------------------------------------

class TestPhaseForbiddenTypes:
    def test_intervals_in_base_flagged(self):
        plan = _make_plan({"wed": ("intervals", 6)})
        warnings = validate_plan(plan, "base")
        assert any("intervals" in w for w in warnings)

    def test_race_pace_long_in_base_flagged(self):
        plan = _make_plan({"sat": ("race_pace_long", 14)})
        warnings = validate_plan(plan, "base")
        assert any("race_pace_long" in w for w in warnings)

    def test_easy_in_base_is_fine(self):
        plan = _make_plan({
            "mon": ("easy", 5),
            "sat": ("long", 12),
        })
        warnings = validate_plan(plan, "base")
        assert not any("base" in w for w in warnings)

    def test_intervals_in_taper_flagged(self):
        plan = _make_plan({"wed": ("intervals", 5)})
        warnings = validate_plan(plan, "taper")
        assert any("intervals" in w for w in warnings)

    def test_hill_repeats_in_taper_flagged(self):
        plan = _make_plan({"tue": ("hill_repeats", 4)})
        warnings = validate_plan(plan, "taper")
        assert any("hill_repeats" in w for w in warnings)

    def test_tempo_in_taper_is_allowed(self):
        # Taper allows 1 sharpening tempo
        plan = _make_plan({
            "tue": ("tempo", 4),
            "sat": ("easy", 6),
        })
        warnings = validate_plan(plan, "taper")
        assert not any("tempo" in w for w in warnings)

    def test_post_race_hard_work_flagged(self):
        plan = _make_plan({"wed": ("tempo", 6)})
        warnings = validate_plan(plan, "post_race")
        assert any("tempo" in w for w in warnings)

    def test_strength_phase_allows_all_types(self):
        plan = _make_plan({
            "tue": ("tempo", 6),
            "thu": ("hill_repeats", 5),
            "sat": ("long", 12),
        })
        warnings = validate_plan(plan, "strength")
        # No forbidden-type warnings (back-to-back and 80/20 may still fire)
        forbidden_warnings = [w for w in warnings if "not appropriate" in w]
        assert not forbidden_warnings

    def test_race_specific_allows_race_pace_long(self):
        plan = _make_plan({
            "mon": ("easy", 5),
            "sat": ("race_pace_long", 14),
        })
        warnings = validate_plan(plan, "race_specific")
        forbidden_warnings = [w for w in warnings if "not appropriate" in w]
        assert not forbidden_warnings


# ---------------------------------------------------------------------------
# Multiple violations in one plan
# ---------------------------------------------------------------------------

class TestMultipleViolations:
    def test_combined_violations_all_reported(self):
        # Back-to-back + phase violation + 80/20 all at once
        plan = _make_plan({
            "mon": ("intervals", 10),   # forbidden in base + starts back-to-back
            "tue": ("tempo", 10),       # forbidden in base + back-to-back + too much hard
            "wed": ("easy", 5),
        })
        warnings = validate_plan(plan, "base")
        assert len(warnings) >= 3

    def test_clean_plan_no_warnings(self):
        plan = _make_plan({
            "mon": ("easy", 5),
            "tue": ("easy", 6),
            "wed": ("hill_repeats", 5),
            "thu": ("easy", 5),
            "fri": ("recovery", 3),
            "sat": ("long", 12),
            "sun": ("easy", 6),
        })
        warnings = validate_plan(plan, "strength")
        assert warnings == []
