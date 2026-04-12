"""
tests/test_periodization.py

Tests for the periodization phase calculator and vert target computation.
No external API calls — pure Python logic.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tools.races import (
    calculate_phase,
    compute_vert_target,
    TAPER_WEEKS,
    RACE_SPECIFIC_WEEKS,
    STRENGTH_WEEKS,
)


def _weeks_out(weeks: float) -> str:
    """Return a race date string exactly N weeks from today."""
    return (datetime.now() + timedelta(weeks=weeks)).strftime("%Y-%m-%d")


def _weeks_ago(weeks: float) -> str:
    return (datetime.now() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Phase boundary tests
# ---------------------------------------------------------------------------

class TestPhaseCalculation:
    def test_base_phase_far_out(self):
        result = calculate_phase(_weeks_out(20))
        assert result["phase"] == "base"
        assert result["weeks_to_race"] >= 12

    def test_base_phase_just_above_threshold(self):
        # 12.5 weeks out → base
        result = calculate_phase(_weeks_out(STRENGTH_WEEKS + 0.5))
        assert result["phase"] == "base"

    def test_strength_phase_at_threshold(self):
        # 12 weeks out → strength (≤ STRENGTH_WEEKS but > RACE_SPECIFIC_WEEKS)
        result = calculate_phase(_weeks_out(STRENGTH_WEEKS))
        assert result["phase"] == "strength"

    def test_strength_phase_midpoint(self):
        result = calculate_phase(_weeks_out(10))
        assert result["phase"] == "strength"

    def test_race_specific_phase(self):
        result = calculate_phase(_weeks_out(5))
        assert result["phase"] == "race_specific"

    def test_race_specific_just_above_taper(self):
        # 3.5 weeks out → race_specific
        result = calculate_phase(_weeks_out(TAPER_WEEKS + 0.5))
        assert result["phase"] == "race_specific"

    def test_taper_phase(self):
        result = calculate_phase(_weeks_out(2))
        assert result["phase"] == "taper"

    def test_taper_phase_at_threshold(self):
        result = calculate_phase(_weeks_out(TAPER_WEEKS))
        assert result["phase"] == "taper"

    def test_taper_one_week_out(self):
        result = calculate_phase(_weeks_out(1))
        assert result["phase"] == "taper"

    def test_post_race(self):
        result = calculate_phase(_weeks_ago(1))
        assert result["phase"] == "post_race"
        assert result["weeks_to_race"] == 0.0  # clamped to 0

    def test_post_race_far_past(self):
        result = calculate_phase(_weeks_ago(4))
        assert result["phase"] == "post_race"

    def test_weeks_to_race_accuracy(self):
        result = calculate_phase(_weeks_out(8))
        assert abs(result["weeks_to_race"] - 8.0) < 0.2  # within 1.5 days margin

    def test_result_has_required_keys(self):
        result = calculate_phase(_weeks_out(10))
        assert "phase" in result
        assert "weeks_to_race" in result
        assert "description" in result
        assert "vert_multiplier" in result

    def test_description_is_non_empty(self):
        for weeks in [20, 10, 5, 2]:
            result = calculate_phase(_weeks_out(weeks))
            assert result["description"], f"Empty description for phase at {weeks} weeks"

    def test_vert_multiplier_is_positive(self):
        for weeks in [20, 10, 5, 2]:
            result = calculate_phase(_weeks_out(weeks))
            assert result["vert_multiplier"] > 0

    def test_vert_multiplier_peaks_in_strength(self):
        strength = calculate_phase(_weeks_out(10))
        base = calculate_phase(_weeks_out(20))
        taper = calculate_phase(_weeks_out(2))
        assert strength["vert_multiplier"] >= base["vert_multiplier"]
        assert strength["vert_multiplier"] >= taper["vert_multiplier"]

    # Compressed schedule: < 12 weeks total → skips base
    def test_compressed_6_weeks_skips_base(self):
        result = calculate_phase(_weeks_out(6))
        assert result["phase"] != "base"

    def test_compressed_3_weeks_is_taper(self):
        result = calculate_phase(_weeks_out(3))
        assert result["phase"] == "taper"


# ---------------------------------------------------------------------------
# Vert target computation
# ---------------------------------------------------------------------------

class TestVertTarget:
    def test_with_known_race_vert_strength_phase(self):
        ctx = {"vert_multiplier": 1.00, "race_elevation_gain_ft": 7000}
        target = compute_vert_target(ctx)
        assert target == int(7000 * 1.2 * 1.00)

    def test_with_known_race_vert_base_phase(self):
        ctx = {"vert_multiplier": 0.60, "race_elevation_gain_ft": 7000}
        target = compute_vert_target(ctx)
        assert target == int(7000 * 1.2 * 0.60)

    def test_with_known_race_vert_taper_phase(self):
        ctx = {"vert_multiplier": 0.50, "race_elevation_gain_ft": 7000}
        target = compute_vert_target(ctx)
        assert target == int(7000 * 1.2 * 0.50)

    def test_without_race_vert_uses_default(self):
        from tools.races import DEFAULT_PEAK_VERT_FT
        ctx = {"vert_multiplier": 0.65, "race_elevation_gain_ft": None}
        target = compute_vert_target(ctx)
        assert target == int(DEFAULT_PEAK_VERT_FT * 0.65)

    def test_without_race_vert_key(self):
        from tools.races import DEFAULT_PEAK_VERT_FT
        ctx = {"vert_multiplier": 1.00}
        target = compute_vert_target(ctx)
        assert target == int(DEFAULT_PEAK_VERT_FT * 1.00)

    def test_target_is_integer(self):
        ctx = {"vert_multiplier": 0.73, "race_elevation_gain_ft": 5500}
        target = compute_vert_target(ctx)
        assert isinstance(target, int)

    def test_higher_race_vert_produces_higher_target(self):
        ctx_low = {"vert_multiplier": 1.0, "race_elevation_gain_ft": 3000}
        ctx_high = {"vert_multiplier": 1.0, "race_elevation_gain_ft": 8000}
        assert compute_vert_target(ctx_high) > compute_vert_target(ctx_low)
