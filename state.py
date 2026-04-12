"""
state.py — Conversation flow state

Tracks whether the next incoming message should be routed as:
  - freeform (default)
  - checkin_reply (athlete responding to evening check-in)
  - missed_workout_reply (athlete explaining a missed workout)
  - post_activity_reply (athlete responding to "how did that feel?")

State is persisted to data/state.json so it survives restarts.
Context (e.g. which activity we just asked about) is stored alongside the flow.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "data" / "state.json"

FLOW_FREEFORM = "freeform"
FLOW_CHECKIN_REPLY = "checkin_reply"
FLOW_MISSED_WORKOUT_REPLY = "missed_workout_reply"
FLOW_POST_ACTIVITY_REPLY = "post_activity_reply"
FLOW_RACE_RESULT = "race_result"


def _load() -> dict:
    if not STATE_FILE.exists():
        return {"flow": FLOW_FREEFORM, "context": {}, "seen_activity_ids": []}
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        if "seen_activity_ids" not in data:
            data["seen_activity_ids"] = []
        return data
    except Exception:
        return {"flow": FLOW_FREEFORM, "context": {}, "seen_activity_ids": []}


def _save(data: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_flow() -> str:
    return _load().get("flow", FLOW_FREEFORM)


def set_flow(flow: str, context: dict | None = None) -> None:
    data = _load()
    data["flow"] = flow
    if context is not None:
        data["context"] = context
    elif flow == FLOW_FREEFORM:
        data["context"] = {}
    _save(data)
    logger.info(f"Flow state → {flow}")


def get_context() -> dict:
    return _load().get("context", {})


def is_activity_seen(activity_id: int) -> bool:
    return activity_id in _load().get("seen_activity_ids", [])


def mark_activity_seen(activity_id: int) -> None:
    data = _load()
    seen = data.get("seen_activity_ids", [])
    if activity_id not in seen:
        seen.append(activity_id)
    # Keep last 50 only
    data["seen_activity_ids"] = seen[-50:]
    _save(data)
