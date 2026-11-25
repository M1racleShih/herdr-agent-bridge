"""Correlate exact prompts with turns in one Codex rollout."""

from __future__ import annotations

import json
from collections import defaultdict


def _text(content, item_types):
    if not isinstance(content, list) or not content:
        return None
    if any(
        not isinstance(item, dict)
        or item.get("type") not in item_types
        or not isinstance(item.get("text"), str)
        for item in content
    ):
        return None
    return "\n".join(item["text"] for item in content)


def _message(entry_type, payload, thread):
    if entry_type == "response_item" and payload.get("type") in (None, "message"):
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        turn = metadata.get("turn_id") if isinstance(metadata, dict) else None
        role = payload.get("role")
        parts = ("input_text", "text", None) if role == "user" else ("output_text", "text", None)
        return turn, role, _text(payload.get("content"), parts), payload.get("phase"), None
    if entry_type == "event_msg" and payload.get("type") == "item_completed":
        item = payload.get("item")
        if payload.get("thread_id") != thread or not isinstance(item, dict):
            return None
        turn = payload.get("turn_id")
        if item.get("type") == "UserMessage":
            return turn, "user", _text(item.get("content"), ("text",)), None, item.get("id")
        if item.get("type") == "AgentMessage":
            return (
                turn,
                "assistant",
                _text(item.get("content"), ("Text", "text")),
                item.get("phase"),
                item.get("id"),
            )
    return None


def extract_path(path, tasks, *, thread_id=None, project=None, lost=False):
    """Read only the specified rollout, leaving incomplete or ambiguous turns unresolved."""
    ready = {"state": "matched"}
    invalid = {"state": "failed", "reason": "codex_rollout_invalid"}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, {"state": "pending", "reason": "codex_rollout_pending"}
    except (OSError, UnicodeError):
        return {}, invalid

    thread = None
    paginated = False
    users = defaultdict(list)
    answers = defaultdict(list)
    completion = defaultdict(list)
    aborted = defaultdict(list)
    context = {}
    settings = []
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except ValueError:
            if index == len(lines) - 1 and not raw.endswith("\n"):
                break  # A live writer can leave an incomplete tail.
            return {}, invalid
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            return {}, invalid
        kind, payload = record.get("type"), record["payload"]
        if kind == "session_meta":
            identity = payload.get("id")
            if (
                not isinstance(identity, str)
                or not identity
                or (thread is not None and identity != thread)
                or (thread_id is not None and identity != thread_id)
                or (project is not None and payload.get("cwd") != project)
            ):
                return {}, {"state": "failed", "reason": "codex_rollout_identity_mismatch"}
            thread = identity
            paginated = payload.get("history_mode") == "paginated"
        if kind == "turn_context" and isinstance(payload.get("turn_id"), str):
            context[payload["turn_id"]] = payload
        if kind == "event_msg":
            event = payload.get("type")
            turn = payload.get("turn_id")
            if event == "thread_rolled_back":
                return {}, {"state": "failed", "reason": "codex_rollout_rewound"}
            if (
                event == "thread_settings_applied"
                and payload.get("thread_id") == thread
                and isinstance(payload.get("thread_settings"), dict)
            ):
                settings.append((index, payload["thread_settings"]))
            if isinstance(turn, str) and turn:
                if event in ("task_complete", "turn_complete"):
                    completion[turn].append((index, payload))
                if event == "turn_aborted":
                    aborted[turn].append((index, payload))
        message = _message(kind, payload, thread)
        if thread is None or message is None:
            continue
        turn, role, body, phase, item_id = message
        if not isinstance(turn, str) or not turn or body is None:
            continue
        if paginated and (kind != "event_msg" or not isinstance(item_id, str) or not item_id):
            continue
        if role == "user":
            users[turn].append((index, body, item_id))
        if role == "assistant":
            answers[turn].append((index, body, phase, item_id))

    if not thread:
        return {}, {"state": "pending", "reason": "codex_rollout_pending"}
    matches = {}
    claimed_turns = set()
    for task in tasks:
        sent = task["submitted_text"]
        bodies = {sent}
        # Codex can record the submitted body without its one terminal LF.
        # Do not broaden this to CRLF, multiple LFs, or whitespace-only input.
        if sent.endswith("\n") and not sent.endswith(("\r\n", "\n\n")) and sent[:-1].strip():
            bodies.add(sent[:-1])
        candidates = [
            (turn, [entry for entry in entries if entry[1] in bodies])
            for turn, entries in users.items()
            if any(entry[1] in bodies for entry in entries)
        ]
        if len(candidates) > 1:
            return {}, {"state": "failed", "reason": "codex_turn_ambiguous"}
        if not candidates:
            continue
        turn, entries = candidates[0]
        # Typed events supersede older response items only for the same body.
        # Distinct typed items and exact/fallback bodies remain ambiguous.
        typed_bodies = {entry[1] for entry in entries if entry[2] is not None}
        entries = [
            entry for entry in entries if entry[2] is not None or entry[1] not in typed_bodies
        ]
        if (
            turn in claimed_turns
            or len(entries) != 1
            or len(completion[turn]) > 1
            or len(aborted[turn]) > 1
        ):
            return {}, {"state": "failed", "reason": "codex_turn_ambiguous"}
        claimed_turns.add(turn)
        user_at, _, _ = entries[0]
        expected = task.get("codex_selection")
        actual = context.get(turn)
        if (
            expected
            and actual
            and (
                actual.get("model") != expected["model"]
                or ("default" if actual.get("effort") in (None, "none") else actual["effort"])
                != expected["thinking"]
                or (project is not None and actual.get("cwd") != project)
            )
        ):
            return {}, {"state": "failed", "reason": "codex_turn_selection_mismatch"}
        service_tier = next(
            (item.get("service_tier") for index, item in reversed(settings) if index < user_at),
            None,
        )
        complete_at, completed = completion[turn][0] if completion[turn] else (-1, {})
        aborted_at, abort = aborted[turn][0] if aborted[turn] else (-1, {})
        if user_at < complete_at and expected and actual is None:
            return {}, {"state": "failed", "reason": "codex_turn_selection_unconfirmed"}
        if (
            user_at < complete_at
            and expected
            and expected.get("service_tier") == "fast"
            and service_tier != "priority"
        ):
            return {}, {"state": "failed", "reason": "codex_turn_fast_unconfirmed"}
        finals = [
            answer
            for answer in answers[turn]
            if user_at < answer[0] and answer[2] == "final_answer"
        ]
        finals = [entry for entry in finals if entry[3] is not None] or finals
        if len(finals) > 1:
            return {}, {"state": "failed", "reason": "codex_turn_ambiguous"}
        final_at, answer, _, item_id = finals[0] if finals else (-1, None, None, None)
        evidence = {
            "thread_id": thread,
            "agent_turn_id": turn,
            "answer": answer,
            "agent_completed": (
                user_at < final_at < complete_at and not completed.get("error") and aborted_at == -1
            ),
        }
        if item_id:
            evidence["final_message_id"] = item_id
        if service_tier == "priority":
            evidence["service_tier"] = "fast"
        if user_at < aborted_at and abort.get("reason") == "interrupted" and not abort.get("error"):
            evidence["agent_aborted"] = True
        recovery = task.get("recovery") or {}
        if aborted_at == complete_at == -1 and (
            lost
            and task is tasks[-1]
            or recovery.get("state") == "restored"
            and recovery.get("thread_id") == thread
            and recovery.get("agent_turn_id") == turn
        ):
            evidence["agent_lost"] = True
        matches[task["id"]] = evidence
    if any(task["id"] not in matches for task in tasks):
        ready = {"state": "pending", "reason": "codex_turn_pending"}
    return matches, ready
