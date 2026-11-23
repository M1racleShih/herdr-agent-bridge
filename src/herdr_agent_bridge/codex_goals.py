"""Read the bound Codex goal and its native turn attribution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tomllib
from contextlib import closing
from pathlib import Path

from herdr_agent_bridge import codex_native
from herdr_agent_bridge.bridge_common import BridgeError

_MAX_ROLLOUT = 64 * 1024 * 1024


def current(bridge, session_id):
    """Return a goal, an empty mapping for no goal, or None for unavailable evidence."""
    home = codex_native.home(bridge.data)
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {}
    except (ValueError, OSError):
        return None
    sqlite_home = config.get("sqlite_home") or bridge.data["native_env"].get("CODEX_SQLITE_HOME")
    if sqlite_home is not None and not isinstance(sqlite_home, str):
        return None
    db = (
        Path(bridge.data["project"])
        / (Path(sqlite_home).expanduser() if sqlite_home else home)
        / "goals_1.sqlite"
    )
    try:
        # A normal read-only connection sees the WAL while the thread is live.
        if db.is_symlink() or not db.is_file():
            return None
        with closing(
            sqlite3.connect(db.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        ) as connection:
            row = connection.execute(
                """SELECT goal_id, objective, status, created_at_ms
                   FROM thread_goals WHERE thread_id = ?""",
                (session_id,),
            ).fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return None
    if row is None:
        return {}
    goal_id, objective, status, created_at_ms = row
    if (
        not isinstance(goal_id, str)
        or not goal_id
        or not isinstance(objective, str)
        or status
        not in {"active", "complete", "paused", "blocked", "usage_limited", "budget_limited"}
        or type(created_at_ms) is not int
    ):
        return None
    return {
        "goal_id": goal_id,
        "objective": objective,
        "goal_status": status,
        "created_at_ms": created_at_ms,
    }


def _rollout(bridge, session_id):
    try:
        path = bridge.native_history()
        if path is None:
            return b""
        with path.open("rb") as stream:
            raw = stream.read(_MAX_ROLLOUT + 1)
        if not raw:
            return raw
        if len(raw) > _MAX_ROLLOUT or not raw.endswith(b"\n"):
            return None
        header = json.loads(raw[: raw.index(b"\n")])
        if (
            header["type"] == "session_meta"
            and header["payload"]["id"] == session_id
            and header["payload"]["cwd"] == bridge.data["project"]
        ):
            return raw
    except (BridgeError, OSError, ValueError, KeyError, TypeError, RecursionError):
        pass
    return None


def before(bridge, session_id):
    raw = _rollout(bridge, session_id)
    if raw is None:
        return None
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _updates(bridge, record):
    raw = _rollout(bridge, record["session_id"])
    snapshot = record["before_rollout"]
    size = snapshot["size"]
    if (
        raw is None
        or len(raw) < size
        or hashlib.sha256(raw[:size]).hexdigest() != snapshot["sha256"]
    ):
        return None
    try:
        lines = raw[size:].splitlines()
        events = []
        for line in lines:
            item = json.loads(line)
            if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                return None
            if item.get("type") == "event_msg":
                events.append(item["payload"])
        return events
    except (ValueError, RecursionError):
        return None


def _matches_goal(event, record, goal):
    thread = record["session_id"]
    details = event.get("goal")
    return (
        event.get("type") == "thread_goal_updated"
        and event.get("threadId") == thread
        and isinstance(details, dict)
        and details.get("threadId") == thread
        and details.get("objective") == record["arguments"]
        and details.get("createdAt") == goal["created_at_ms"] // 1000
        and details.get("status") == "active"
        and not event.get("turnId")
    )


def _turns(events):
    starts, ends = [], set()
    for event in events:
        kind, turn = event.get("type"), event.get("turn_id")
        if kind in {"task_started", "turn_started"}:
            attribution = event.get("turn_attribution")
            if (
                not isinstance(turn, str)
                or not turn
                or turn in starts
                or not isinstance(attribution, dict)
                or attribution.get("turn_id") != turn
                or attribution.get("turn_trigger") != "goal"
            ):
                return None
            starts.append(turn)
        elif kind in {"task_complete", "turn_complete", "turn_aborted"}:
            if not isinstance(turn, str) or turn not in starts or turn in ends:
                return None
            ends.add(turn)
        elif kind == "thread_rolled_back":
            return None
    return starts, ends


def observe(bridge, record):
    """Pair the native goal mutation and any goal-generated turns after the intent."""
    goal = current(bridge, record["session_id"])
    if (
        not goal
        or goal["objective"] != record["arguments"]
        or goal["goal_id"] == record["previous_goal_id"]
        or record.get("goal_id", goal["goal_id"]) != goal["goal_id"]
    ):
        return None
    events = _updates(bridge, record)
    if events is None:
        return None
    matches = [index for index, event in enumerate(events) if _matches_goal(event, record, goal)]
    if len(matches) != 1 or any(
        event.get("type") in {"task_started", "turn_started"} for event in events[: matches[0]]
    ):
        return None
    turns = _turns(events[matches[0] + 1 :])
    if turns is None:
        return None
    started, ended = turns
    return (
        goal["goal_id"],
        {
            "source": "codex_rollout_and_goal_state",
            "goal_status": goal["goal_status"],
            "goal_turn_ids": started,
        },
        bool(started) and len(started) == len(ended),
    )
