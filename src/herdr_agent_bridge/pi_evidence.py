"""Extract conservative turn evidence from one dedicated Pi v3 session.

This reads only the caller's isolated Pi home. Pi's JSONL is a version-specific
implementation detail, not a stable API or proof that the task was successful.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from herdr_agent_bridge.pi_team_evidence import continuation as team_continuation

_STOP_REASONS = {"pending", "stop", "length", "toolUse", "error", "aborted", "deferred"}
_STATE_ENTRIES = {
    "model_change",
    "thinking_level_change",
    "usage",
    "custom",
    "label",
    "session_info",
}
# ECMAScript String.trim() whitespace and line terminators. Python's default
# strip() includes additional characters, so it would permit false matches.
_JS_TRIM = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _nonempty(value):
    return isinstance(value, str) and bool(value)


def _timestamp(value):
    return not isinstance(value, bool) and (
        isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-JSON numeric constant")


def _text_content(content):
    """Return exact text, accepting only Pi's string or text-block form."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return None
    if any(
        not isinstance(block, dict)
        or block.get("type") != "text"
        or not isinstance(block.get("text"), str)
        for block in content
    ):
        return None
    return "".join(block["text"] for block in content)


def _unsupported_blocks(content, allowed):
    return isinstance(content, list) and any(
        isinstance(block, dict) and _nonempty(block.get("type")) and block["type"] not in allowed
        for block in content
    )


def _content_failure(message, allowed):
    return (
        "native_structure_unsupported"
        if _unsupported_blocks(message.get("content"), allowed)
        else "native_record_malformed"
    )


def _assistant(message):
    """Return (valid, final text). Unfinished/non-final output has no text."""
    if (
        not all(_nonempty(message.get(key)) for key in ("api", "provider", "model"))
        or not isinstance(message.get("usage"), dict)
        or not isinstance(message.get("stopReason"), str)
        or message.get("stopReason") not in _STOP_REASONS
        or ("endTurn" in message and not isinstance(message["endTurn"], bool))
    ):
        return False, None
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return False, None
    texts, tool_call = [], False
    for block in blocks:
        if not isinstance(block, dict):
            return False, None
        kind = block.get("type")
        if kind == "text":
            if not isinstance(block.get("text"), str):
                return False, None
            texts.append(block["text"])
        elif kind == "thinking":
            thinking = block.get("thinking")
            if not isinstance(thinking, str) and not (
                thinking is None and block.get("redacted") is True
            ):
                return False, None
        elif kind == "toolCall":
            if (
                not _nonempty(block.get("id"))
                or not _nonempty(block.get("name"))
                or not isinstance(block.get("arguments"), dict)
            ):
                return False, None
            tool_call = True
        else:
            return False, None
    if (
        message["stopReason"] == "stop"
        and texts
        and not tool_call
        and message.get("endTurn") is not False
    ):
        return True, "".join(texts)
    return True, None


def _tool_result(message):
    if (
        not _nonempty(message.get("toolCallId"))
        or not _nonempty(message.get("toolName"))
        or not isinstance(message.get("isError"), bool)
        or not isinstance(message.get("content"), list)
    ):
        return False
    for block in message["content"]:
        if not isinstance(block, dict):
            return False
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            continue
        if (
            block.get("type") == "image"
            and isinstance(block.get("data"), str)
            and _nonempty(block.get("mimeType"))
        ):
            continue
        return False
    return True


def _other_entry(entry):
    """Allow only understood entries that cannot add conversation messages."""
    kind = entry.get("type")
    if kind == "model_change":
        return _nonempty(entry.get("provider")) and _nonempty(entry.get("modelId"))
    if kind == "thinking_level_change":
        return _nonempty(entry.get("thinkingLevel"))
    if kind == "usage":
        return all(_nonempty(entry.get(k)) for k in ("kind", "provider", "model")) and isinstance(
            entry.get("usage"), dict
        )
    if kind == "custom":
        return _nonempty(entry.get("customType"))
    if kind == "label":
        return _nonempty(entry.get("targetId")) and (
            "label" not in entry or isinstance(entry["label"], str)
        )
    if kind == "session_info":
        return "name" not in entry or isinstance(entry["name"], str)
    # Includes compaction, context edits, branches, and context-injecting custom
    # messages: their relationship to a submitted task is not supported here.
    return False


def _empty(audit, state, reason):
    if audit is not None:
        audit.update(state=state, reason=reason)
    return {}


def _extract_turns(home, tasks, *, selection=None, _audit=None):
    """Match Pi editor-trimmed submitted text to native entries in a linear session.

    Return no correlations for multiple sessions, malformed/unsupported data,
    forks or branches. An unfinished trailing write cannot complete the current
    turn; already completed turns bounded by a later user remain available.
    """
    try:
        session_dir = Path(home) / "sessions"
        if session_dir.is_symlink():
            return _empty(_audit, "failed", "native_session_unsupported")
        paths = list(session_dir.rglob("*.jsonl"))
        if not paths:
            return _empty(_audit, "pending", "native_session_missing")
        if len(paths) != 1:
            return _empty(_audit, "failed", "native_session_ambiguous")
        if paths[0].is_symlink() or not paths[0].is_file():
            return _empty(_audit, "failed", "native_session_unsupported")
        if not paths[0].resolve().is_relative_to(session_dir.resolve()):
            return _empty(_audit, "failed", "native_session_unsupported")
        data = paths[0].read_bytes()
    except (TypeError, ValueError):
        return _empty(_audit, "failed", "native_input_invalid")
    except (OSError, RuntimeError):
        return _empty(_audit, "pending", "native_session_unavailable")

    # Pi appends each record with a newline. Ignore an unterminated write even
    # if it happens to be valid JSON; more bytes might still change its meaning.
    lines = data.split(b"\n")
    partial_tail = bool(lines.pop())
    if not lines:
        reason = "native_trailing_write" if partial_tail else "native_session_incomplete"
        return _empty(_audit, "pending", reason)
    try:
        entries = [
            json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_invalid_constant,
            )
            for line in lines
        ]
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _empty(_audit, "failed", "native_record_malformed")

    header = entries[0]
    if (
        not isinstance(header, dict)
        or not _nonempty(header.get("type"))
        or type(header.get("version")) is not int
        or not all(_nonempty(header.get(k)) for k in ("id", "cwd", "timestamp"))
    ):
        return _empty(_audit, "failed", "native_record_malformed")
    if header["version"] != 3:
        return _empty(_audit, "failed", "native_version_unsupported")
    if header["type"] != "session" or "parentSession" in header:
        return _empty(_audit, "failed", "native_structure_unsupported")
    if selection is not None and header["id"] != selection["session_id"]:
        _audit.update(state="failed", reason="native_session_mismatch")
        return {}

    turns, seen, previous = [], set(), None
    selected_model = selected_thinking = False
    expected_model = tuple(selection["model"].split("/", 1)) if selection is not None else None
    for entry in entries[1:]:
        if (
            not isinstance(entry, dict)
            or not _nonempty(entry.get("id"))
            or "parentId" not in entry
            or not _nonempty(entry.get("timestamp"))
        ):
            return _empty(_audit, "failed", "native_record_malformed")
        if entry["id"] in seen or entry["parentId"] != previous:
            return _empty(_audit, "failed", "native_history_ambiguous")
        seen.add(entry["id"])
        previous = entry["id"]
        if entry.get("type") != "message":
            if _nonempty(entry.get("type")) and entry["type"] not in _STATE_ENTRIES:
                return _empty(_audit, "failed", "native_structure_unsupported")
            if not _other_entry(entry):
                return _empty(_audit, "failed", "native_record_malformed")
            if selection is not None and entry["type"] == "model_change":
                if (entry["provider"], entry["modelId"]) != expected_model:
                    _audit.update(state="failed", reason="native_selection_mismatch")
                    return {}
                selected_model = True
            if selection is not None and entry["type"] == "thinking_level_change":
                if entry["thinkingLevel"] != selection["thinking"]:
                    _audit.update(state="failed", reason="native_selection_mismatch")
                    return {}
                selected_thinking = True
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or not _timestamp(message.get("timestamp")):
            return _empty(_audit, "failed", "native_record_malformed")
        role = message.get("role")
        if role == "user":
            if selection is not None and not (selected_model and selected_thinking):
                _audit.update(state="failed", reason="native_selection_unavailable")
                return {}
            text = _text_content(message.get("content"))
            if text is None:
                return _empty(_audit, "failed", _content_failure(message, {"text"}))
            turns.append(
                {"id": entry["id"], "text": text, "final": None, "aborted": None, "events": []}
            )
        elif role == "assistant":
            valid, answer = _assistant(message)
            if not valid:
                if (
                    _nonempty(message.get("stopReason"))
                    and message["stopReason"] not in _STOP_REASONS
                ):
                    return _empty(_audit, "failed", "native_structure_unsupported")
                return _empty(
                    _audit, "failed", _content_failure(message, {"text", "thinking", "toolCall"})
                )
            if selection is not None and (message["provider"], message["model"]) != expected_model:
                _audit.update(state="failed", reason="native_dispatch_mismatch")
                return {}
            if turns:
                turns[-1]["events"].append(message)
                turns[-1]["final"] = (entry["id"], answer) if answer is not None else None
                turns[-1]["aborted"] = entry["id"] if message["stopReason"] == "aborted" else None
        elif role == "toolResult":
            if not _tool_result(message):
                return _empty(_audit, "failed", _content_failure(message, {"text", "image"}))
            if turns:
                turns[-1]["events"].append(message)
                turns[-1]["final"] = None
                turns[-1]["aborted"] = None
        elif role == "system":
            if _text_content(message.get("content")) is None:
                return _empty(_audit, "failed", _content_failure(message, {"text"}))
            if turns:
                turns[-1]["events"].append(message)
        else:
            reason = (
                "native_structure_unsupported" if _nonempty(role) else "native_record_malformed"
            )
            return _empty(_audit, "failed", reason)

    if partial_tail and turns:
        turns[-1]["final"] = None
        turns[-1]["aborted"] = None

    # Pi 1.5's interactive editor trims before sending. Keep the original
    # submitted text intact, and reject collisions after this transformation.
    # A repeated native user message likewise makes that task ambiguous.
    try:
        task_ids, task_texts = {}, {}
        for task in tasks:
            if (
                not isinstance(task, dict)
                or not _nonempty(task.get("id"))
                or not isinstance(task.get("submitted_text"), str)
            ):
                return {}
            task_ids[task["id"]] = task_ids.get(task["id"], 0) + 1
            text = task["submitted_text"].strip(_JS_TRIM)
            task_texts[text] = task_texts.get(text, 0) + 1
    except TypeError:
        return {}

    # Detect native collisions conservatively, but require the chosen native
    # text itself to equal the editor output; never normalize it into a match.
    native_texts = {}
    for turn in turns:
        native_texts.setdefault(turn["text"].strip(_JS_TRIM), []).append(turn)
    result = {}
    ambiguous = False
    for task in tasks:
        text = task["submitted_text"].strip(_JS_TRIM)
        matches = native_texts.get(text, [])
        if not text or task_ids[task["id"]] != 1 or task_texts[text] != 1 or len(matches) > 1:
            ambiguous = True
        if (
            not text
            or task_ids[task["id"]] != 1
            or task_texts[text] != 1
            or len(matches) != 1
            or matches[0]["text"] != text
        ):
            continue
        turn = matches[0]
        final = turn["final"]
        if selection is not None and final is None and not turn["aborted"]:
            final = team_continuation(turns, turns.index(turn), task, task_texts, header["cwd"])
        evidence = {
            "thread_id": header["id"],
            "agent_turn_id": turn["id"],
            "answer": final[1] if final else None,
            "agent_completed": final is not None,
            "turn_id_source": "pi_user_message_id",
        }
        if final:
            evidence["final_message_id"] = final[0]
        elif turn["aborted"]:
            evidence.update(agent_aborted=True, aborted_message_id=turn["aborted"])
        recovery = task.get("recovery") or {}
        if (
            not final
            and not turn["aborted"]
            and recovery.get("state") == "restored"
            and recovery.get("thread_id") == header["id"]
            and recovery.get("agent_turn_id") == turn["id"]
        ):
            evidence["agent_lost"] = True
        result[task["id"]] = evidence
    if _audit is not None:
        if ambiguous:
            _audit.update(state="failed", reason="native_input_ambiguous")
        elif partial_tail:
            _audit.update(state="pending", reason="native_trailing_write")
        elif not (selected_model and selected_thinking):
            _audit.update(state="pending", reason="native_selection_pending")
        elif not tasks or len(result) != len(tasks):
            _audit.update(state="pending", reason="native_no_match")
        elif any(
            not (
                evidence["agent_completed"]
                or evidence.get("agent_aborted")
                or evidence.get("agent_lost")
            )
            for evidence in result.values()
        ):
            _audit.update(state="pending", reason="native_turn_incomplete")
        else:
            _audit.update(state="matched")
    return result


def extract_turns(home, tasks):
    """Extract legacy Pi turn evidence without an explicit selection policy."""
    return _extract_turns(home, tasks)


def extract_selected_turns(home, tasks, selection):
    """Return (matches, audit) for a selected native session.

    The audit is {'state': 'matched'} only when every requested task has a
    completed native turn. Pending and failed states carry a fixed 'reason'
    code with no session content or path. Matches may include independently
    bounded turns even when the audit is pending or another input is ambiguous;
    callers must evaluate the audit before using them as a selected result.
    Native completion alone is not proof of task success.
    """
    audit = {"state": "pending"}
    if (
        not isinstance(selection, dict)
        or not all(_nonempty(selection.get(key)) for key in ("session_id", "model", "thinking"))
        or "/" not in selection["model"]
        or not all(selection["model"].split("/", 1))
        or not isinstance(tasks, (list, tuple))
        or any(
            not isinstance(task, dict)
            or not _nonempty(task.get("id"))
            or not isinstance(task.get("submitted_text"), str)
            for task in tasks
        )
    ):
        return _empty(audit, "failed", "native_input_invalid"), audit
    ids = [task["id"] for task in tasks]
    texts = [task["submitted_text"].strip(_JS_TRIM) for task in tasks]
    matches = _extract_turns(home, tasks, selection=selection, _audit=audit)
    if audit["state"] == "pending" and (
        not all(texts) or len(set(ids)) != len(ids) or len(set(texts)) != len(texts)
    ):
        audit.update(state="failed", reason="native_input_ambiguous")
    return matches, audit
