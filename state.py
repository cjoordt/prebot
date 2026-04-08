"""
state.py — Conversation flow state

Tracks whether the next incoming message should be routed as:
  - freeform (default)
  - checkin_reply (athlete responding to evening check-in)
  - missed_workout_reply (athlete explaining a missed workout)

State is persisted to data/state.json so it survives restarts.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "data" / "state.json"

FLOW_FREEFORM = "freeform"
FLOW_CHECKIN_REPLY = "checkin_reply"
FLOW_MISSED_WORKOUT_REPLY = "missed_workout_reply"


def get_flow() -> str:
    """Return the current conversation flow state."""
    if not STATE_FILE.exists():
        return FLOW_FREEFORM
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f).get("flow", FLOW_FREEFORM)
    except Exception:
        return FLOW_FREEFORM


def set_flow(flow: str) -> None:
    """Persist the conversation flow state."""
    with open(STATE_FILE, "w") as f:
        json.dump({"flow": flow}, f)
    logger.info(f"Flow state → {flow}")
