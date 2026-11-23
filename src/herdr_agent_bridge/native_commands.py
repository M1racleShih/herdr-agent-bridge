"""Journal and reconcile terminal commands separately from ordinary task rounds."""

from __future__ import annotations

import json
import re
import subprocess
import time

from herdr_agent_bridge import codex_goals, codex_native
from herdr_agent_bridge.agent_capabilities import AGENT_CAPABILITIES
from herdr_agent_bridge.bridge_common import BridgeError, HcError, fsync_directory
from herdr_agent_bridge.terminal_recovery import observe_process

_READY = {"idle", "done"}
_PENDING = {"delivery_unknown", "goal_active", "pending_selection"}


def _validate(operation_id, command, arguments):
    if (
        not isinstance(operation_id, str)
        or re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", operation_id) is None
        or not isinstance(command, str)
        or re.fullmatch(r"/[a-z][a-z0-9-]{0,31}", command) is None
        or arguments is not None
        and (
            not isinstance(arguments, str)
            or len(arguments) > 4000
            or arguments
            and not arguments.isprintable()
        )
    ):
        raise BridgeError(
            "invalid native operation id, command or arguments", code="native_command_invalid"
        )
    value = arguments.strip() if arguments else None
    if command == "/name" and (not value or len(value) > 80):
        raise BridgeError(
            "/name requires a title of at most 80 characters", code="native_command_invalid"
        )
    if command == "/fast":
        if value not in (None, "on"):
            raise BridgeError("/fast accepts only on", code="native_command_invalid")
        return "on"
    if command == "/goal" and (not value or value.lower() in {"clear", "edit", "pause", "resume"}):
        raise BridgeError("/goal requires a new objective", code="native_command_invalid")
    if command == "/model" and value is not None:
        raise BridgeError(
            "/model opens the native selector without arguments", code="native_command_invalid"
        )
    return value


def public_operation(record):
    if record is None:
        return None
    return {
        key: record[key]
        for key in (
            "operation_id",
            "command",
            "arguments",
            "state",
            "reason",
            "delivery",
            "session_id",
            "evidence",
        )
        if key in record
    }


def latest_operation(data):
    operations = data.get("native_operations", {})
    return next(reversed(operations.values()), None)


def pending_control(data):
    latest = latest_operation(data)
    return latest if latest is not None and latest["state"] in _PENDING else None


def goal_follows(data, task_id):
    """Report whether a native goal superseded the latest round's worktree."""
    return any(
        operation["command"] == "/goal" and operation.get("before_task_id") == task_id
        for operation in data.get("native_operations", {}).values()
    )


def _screen(bridge, terminal):
    try:
        result = bridge.hc("read", "--json", "--lines", "0", terminal["address"])
    except (HcError, subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if (
        isinstance(result, dict)
        and result.get("complete") is True
        and result.get("how") == "screen"
        and isinstance(result.get("text"), str)
    ):
        return [line.strip() for line in result["text"].splitlines()]
    return None


def _pi_name(bridge, session_id):
    path = bridge.root / "pi-name.json"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            return None
        value = json.loads(raw)
        if (
            isinstance(value, dict)
            and value.get("session_id") == session_id
            and isinstance(value.get("revision"), str)
            and (value.get("name") is None or isinstance(value.get("name"), str))
        ):
            return value
    except (OSError, ValueError, RecursionError):
        pass
    return None


def _fast_supported(bridge, model):
    try:
        with (codex_native.home(bridge.data) / "models_cache.json").open("rb") as stream:
            raw = stream.read(8_388_609)
        if len(raw) > 8_388_608:
            return None
        models = json.loads(raw)["models"]
        matches = [entry for entry in models if entry["slug"] == model]
        if len(matches) != 1:
            return None
        model = matches[0]
        if "service_tiers" in model:
            return any(tier["name"].casefold() == "fast" for tier in model["service_tiers"])
        return "fast" in model["additional_speed_tiers"]
    except (KeyError, OSError, TypeError, ValueError, AttributeError):
        return None


def _model_menu(screen):
    return screen is not None and any(
        line in ("Select Model", "Select Model and Effort")
        or line.startswith("Select Reasoning Level for ")
        for line in screen
    )


def _report(record, state, reason):
    return {
        **public_operation(record),
        "state": state,
        "reason": reason,
        "delivery": "not_attempted",
    }


def _precheck(bridge, terminal, record, selection):
    command = record["command"]
    if command == "/fast":
        tier = selection.get("service_tier")
        if tier == "fast":
            record.update(
                state="confirmed",
                delivery="not_attempted",
                evidence={
                    "source": "codex_terminal",
                    "service_tier": "fast",
                    "already_enabled": True,
                },
            )
        elif not tier:
            return _report(record, "not_ready", "native_fast_state_unknown")
        else:
            supported = _fast_supported(bridge, selection["model"])
            if supported is False:
                return _report(record, "unsupported", "native_model_fast_unsupported")
            if supported is None:
                return _report(record, "not_ready", "native_model_capabilities_unavailable")
    elif command == "/goal":
        goal = codex_goals.current(bridge, selection["session_id"])
        before = codex_goals.before(bridge, selection["session_id"])
        if goal is None or before is None:
            return _report(record, "not_ready", "native_goal_evidence_unavailable")
        if goal and goal["goal_status"] != "complete":
            reason = (
                "native_goal_active"
                if goal["goal_status"] == "active"
                else "native_goal_requires_attention"
            )
            return _report(record, "not_ready", reason)
        record.update(previous_goal_id=goal.get("goal_id"), before_rollout=before)
    elif command == "/name":
        screen = _screen(bridge, terminal)
        if screen is None or not any(screen):
            return _report(record, "not_ready", "native_evidence_unavailable")
        name = _pi_name(bridge, selection["session_id"])
        if name is not None:
            if name.get("name") == record["arguments"]:
                record.update(
                    state="confirmed",
                    delivery="not_attempted",
                    evidence={
                        "source": "pi_session",
                        "session_name": record["arguments"],
                        "already_set": True,
                    },
                )
            record["before_name_revision"] = name["revision"]
        else:
            record["before_name_count"] = screen.count("Session name set: " + record["arguments"])
    elif command == "/model":
        screen = _screen(bridge, terminal)
        if screen is None or _model_menu(screen):
            return _report(record, "not_ready", "native_composer_unavailable")
    return None


def _mark_confirmed(record, evidence):
    record.update(state="confirmed", delivery="confirmed", evidence=evidence)
    record.pop("reason", None)


def _refresh(bridge, terminal, record, selection):
    state = (terminal.get("status") or {}).get("state")
    record["reason"] = "native_evidence_pending"
    if terminal["state"] != "live":
        record["reason"] = "native_terminal_unavailable"
        return
    command = record["command"]
    if selection is not None and selection.get("state") == "failed":
        record.update(state="delivery_unknown", reason="native_session_changed_or_unavailable")
        record.pop("evidence", None)
        return
    if command == "/model":
        screen = _screen(bridge, terminal)
        if _model_menu(screen):
            record.update(
                state="pending_selection",
                delivery="confirmed",
                reason="native_model_selection",
                menu_observed=True,
            )
            record["evidence"] = {"source": "codex_terminal"}
            return
    if state == "blocked" or codex_native.trust_required(selection):
        record["reason"] = "native_terminal_blocked"
        return
    if command == "/goal":
        effect = codex_goals.observe(bridge, record)
        if effect is None:
            record.update(state="delivery_unknown", reason="native_goal_evidence_pending")
            record.pop("evidence", None)
            return
        record["goal_id"], record["evidence"], turns_finished = effect
        record["delivery"] = "confirmed"
        goal_status = record["evidence"]["goal_status"]
        if goal_status != "complete" or not turns_finished or state not in _READY:
            record.update(
                state="goal_active" if goal_status == "active" else "delivery_unknown",
                reason="native_goal_active" if goal_status == "active" else "native_goal_unsettled",
            )
            return
    if (
        selection is None
        or selection.get("state") != "ready"
        or selection.get("session_id") != record["session_id"]
    ):
        record["reason"] = "native_session_changed_or_unavailable"
        return
    if command == "/goal":
        _mark_confirmed(record, record["evidence"])
    elif command == "/fast":
        if (
            state in _READY
            and selection.get("service_tier") == "fast"
            and terminal["turn"] == record["before_turn"]
        ):
            _mark_confirmed(
                record,
                {
                    "source": "codex_terminal",
                    "service_tier": "fast",
                    "already_enabled": False,
                },
            )
    elif command == "/model":
        if (
            record.get("menu_observed")
            and screen is not None
            and state in _READY
            and terminal["turn"] == record["before_turn"]
        ):
            _mark_confirmed(record, {"source": "codex_terminal", "selection_closed": True})
    elif command == "/name" and state in _READY and terminal["turn"] == record["before_turn"]:
        if "before_name_revision" in record:
            name = _pi_name(bridge, record["session_id"])
            if (
                name is not None
                and name["revision"] != record["before_name_revision"]
                and name.get("name") == record["arguments"]
            ):
                _mark_confirmed(
                    record, {"source": "pi_session", "session_name": record["arguments"]}
                )
        else:
            screen = _screen(bridge, terminal)
            notice = "Session name set: " + record["arguments"]
            if screen is not None and screen.count(notice) > record["before_name_count"]:
                _mark_confirmed(
                    record, {"source": "pi_terminal", "session_name": record["arguments"]}
                )


def observe_controls(bridge, terminal, *, selection=None):
    """Refresh a delivered operation under the same lock as ordinary input."""
    record = pending_control(bridge.data)
    if record is not None:
        selection = selection or bridge.agent_selection(terminal=terminal)
        _refresh(bridge, terminal, record, selection)
    return pending_control(bridge.data)


def _fast_selected(operation, selection):
    return (
        selection is not None
        and selection.get("state") == "ready"
        and selection.get("session_id") == operation["session_id"]
        and selection.get("service_tier") == "fast"
    )


def fast_ready(data, selection):
    fast = next(
        (
            record
            for record in reversed(data.get("native_operations", {}).values())
            if record["command"] == "/fast" and record["state"] == "confirmed"
        ),
        None,
    )
    return fast is None or _fast_selected(fast, selection)


def assert_ordinary_input(bridge, terminal, *, selection=None):
    previous = observe_controls(bridge, terminal, selection=selection)
    if previous is not None:
        bridge.persist()
        raise BridgeError(
            "native operation is unresolved; inspect its original operation id",
            code="native_control_pending",
        )
    if not fast_ready(bridge.data, selection):
        raise BridgeError(
            "Fast is not confirmed in the current session; enable it before submitting",
            code="native_fast_required",
        )
    if bridge.native_codex():
        selection = selection or bridge.agent_selection(terminal=terminal)
        goal = (
            codex_goals.current(bridge, selection["session_id"])
            if selection.get("session_id")
            else None
        )
        if goal and goal["goal_status"] == "active":
            raise BridgeError("the current native goal is active", code="native_control_pending")


def execute(
    bridge, operation_id, command, arguments=None, *, development_task=None, input_ready=None
):
    arguments = _validate(operation_id, command, arguments)
    result = {"operation_id": operation_id, "command": command, "arguments": arguments}
    with bridge.locked():
        dev = bridge.development()
        if dev:
            dev.assert_input(development_task, False)
        elif development_task is not None:
            raise BridgeError("not a development run")
        operations = bridge.data.get("native_operations", {})
        existing = operations.get(operation_id)
        if existing is not None and (existing["command"], existing["arguments"]) != (
            command,
            arguments,
        ):
            raise BridgeError(
                "native operation id is already bound to other input",
                code="native_operation_conflict",
            )
        capabilities = AGENT_CAPABILITIES[bridge.agent()]
        if bridge.data.get("runtime") != "native" or command not in capabilities.native_commands:
            return {**result, "state": "unsupported", "delivery": "not_attempted"}
        operations = bridge.data.setdefault("native_operations", {})
        if existing is not None and existing["state"] not in _PENDING and command != "/fast":
            return public_operation(existing)
        if bridge.data["launch"] not in ("submitted", "observed"):
            return _report(result, "not_ready", "launch_unreconciled")
        terminal = bridge.hc("get", "--json", bridge.target())
        bridge.identity(terminal)
        observe_process(bridge, terminal)
        bridge.data["terminal_snapshot"] = terminal
        selection = bridge.agent_selection(terminal=terminal)
        if existing is not None:
            if existing["state"] in _PENDING:
                _refresh(bridge, terminal, existing, selection)
            bridge.persist()
            recorded = public_operation(existing)
            if (
                command == "/fast"
                and existing["state"] == "confirmed"
                and not _fast_selected(existing, selection)
            ):
                return {**recorded, "state": "not_ready", "reason": "native_fast_drifted"}
            return recorded

        state = (terminal.get("status") or {}).get("state")
        reason = None
        if observe_controls(bridge, terminal, selection=selection) is not None:
            reason = "native_control_pending"
        elif (bridge.data.get("recovery") or {}).get("state") in ("needed", "launch_unknown"):
            reason = "recovery_required"
        elif codex_native.trust_required(selection):
            reason = "native_terminal_blocked"
        elif terminal["state"] != "live" or state not in _READY:
            reason = (
                "native_terminal_blocked" if state == "blocked" else "native_terminal_not_ready"
            )
        elif input_ready is not None and not input_ready(terminal):
            reason = "startup_not_ready"
        elif bridge.data["tasks"] and not bridge.continuable(bridge.data["tasks"][-1]):
            reason = "ordinary_round_unresolved"
        elif (
            selection is None
            or selection.get("state") != "ready"
            or not selection.get("session_id")
        ):
            reason = "native_session_unconfirmed"
        elif bridge.data["tasks"] and selection.get("native_evidence") != "matched":
            reason = "ordinary_round_evidence_unconfirmed"
        if reason is not None:
            bridge.persist()
            return _report(result, "not_ready", reason)
        result["session_id"] = selection["session_id"]
        if bridge.native_codex() and command != "/goal":
            goal = codex_goals.current(bridge, selection["session_id"])
            if goal and goal["goal_status"] == "active":
                bridge.persist()
                return _report(result, "not_ready", "native_goal_active")
        refusal = _precheck(bridge, terminal, result, selection)
        if refusal is not None:
            bridge.persist()
            return refusal
        if result.get("state") == "confirmed":
            operations[operation_id] = result
            bridge.persist()
            fsync_directory(bridge.root)
            return public_operation(result)
        result.update(
            state="delivery_unknown",
            delivery="unknown",
            reason="native_evidence_pending",
            session_id=selection["session_id"],
            before_turn=terminal["turn"],
            requested_at=time.time(),
        )
        operations[operation_id] = result
        if command == "/goal" and bridge.data["tasks"]:
            latest = bridge.data["tasks"][-1]
            result["before_task_id"] = latest["id"]
            latest["verified_success"] = False
            latest.pop("verification_basis", None)
            latest.pop("verification", None)
        if dev:
            dev.invalidate()
        bridge.persist()
        fsync_directory(bridge.root)
        text = command + (" " + arguments if arguments and command != "/fast" else "")
        try:
            result["receipt"] = bridge.hc("send", "--json", terminal["address"], text)
        except (HcError, subprocess.TimeoutExpired, OSError, ValueError):
            pass  # The command may already have reached the terminal.
        try:
            current = bridge.hc("get", "--json", bridge.target())
            bridge.identity(current)
            bridge.data["terminal_snapshot"] = current
            _refresh(bridge, current, result, bridge.agent_selection(terminal=current))
        except (BridgeError, subprocess.TimeoutExpired, OSError, ValueError):
            pass  # The durable operation remains unknown until a later read-back.
        bridge.persist()
        return public_operation(result)
