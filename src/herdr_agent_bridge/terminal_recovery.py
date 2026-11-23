"""Native process-loss recovery; callers own Bridge locking and persistence."""

import json
import secrets
import shlex
import subprocess
import time
from pathlib import Path

from herdr_agent_bridge import codex_native
from herdr_agent_bridge.agent_adapters import build_agent_command
from herdr_agent_bridge.bridge_common import BridgeError, HcError, digest, fsync_directory
from herdr_agent_bridge.bridge_protocol import verification_basis
from herdr_agent_bridge.development import fingerprint, snapshot


def observe_process(bridge, terminal):
    if bridge.data.get("runtime") != "native":
        return
    pid = terminal.get("pid")
    previous = bridge.data.get("agent_pid") or (bridge.data.get("terminal_snapshot") or {}).get(
        "pid"
    )
    previous_argv = bridge.data.get("agent_argv") or (
        bridge.data.get("terminal_snapshot") or {}
    ).get("argv")
    recovery = bridge.data.get("recovery") or {}
    if recovery.get("state") in ("needed", "launch_unknown"):
        return
    if previous is None:
        if pid:
            bridge.data["agent_pid"] = pid
            bridge.data["agent_argv"] = terminal.get("argv")
    elif pid != previous or previous_argv is not None and terminal.get("argv") != previous_argv:
        bridge.data["recovery"] = {"state": "needed", "original_pid": previous}
        dev = bridge.development()
        if dev:
            dev.request_cancellation()
            dev.invalidate()
        for task in bridge.data["tasks"]:
            task["verified_success"] = False


def review_snapshot(bridge, terminal):
    session = bridge.native_history()
    if session is None:
        raise BridgeError("one intact native session is required", code="recovery_session_invalid")
    files = snapshot(bridge.data["project"])
    value = {
        "files": files,
        "session_sha256": digest(session.read_text()),
        "address": terminal["address"],
        "pid": terminal.get("pid"),
        "argv": terminal.get("argv"),
        "rounds": [(task["id"], task["input_sha256"]) for task in bridge.data["tasks"]],
    }
    if bridge.native_codex():
        value["thread_id"] = bridge.data["codex_binding"]["session_id"]
    return {
        "fingerprint": fingerprint(value),
        "files": files,
        "session_file": str(session),
        "session_sha256": value["session_sha256"],
    }


def summary(record):
    return {
        key: record[key]
        for key in ("state", "actor", "review", "fingerprint", "reason")
        if key in record
    }


def start(bridge, terminal, expected_fingerprint, actor, review):
    if (
        bridge.data.get("runtime") != "native"
        or not isinstance(actor, str)
        or not actor.strip()
        or len(actor) > 256
        or not isinstance(review, str)
        or not review.strip()
        or len(review) > 4096
    ):
        raise BridgeError("native recovery requires an actor and review", code="recovery_invalid")
    record = bridge.data.get("recovery") or {}
    if record.get("state") in ("launch_unknown", "restored"):
        return summary(record)
    if record.get("state") != "needed":
        raise BridgeError("no agent loss has been observed", code="recovery_not_needed")
    argv = terminal.get("argv") or []
    if (
        terminal.get("state") != "live"
        or not terminal.get("keep")
        or not terminal.get("pid")
        or not argv
        or Path(argv[0]).name not in ("sh", "bash", "zsh", "dash")
        or (terminal.get("status") or {}).get("state") is not None
    ):
        raise BridgeError(
            "wait for the kept terminal to recover its shell", code="recovery_shell_pending"
        )
    current = review_snapshot(bridge, terminal)
    if current["fingerprint"] != expected_fingerprint:
        raise BridgeError(
            "code, session or terminal changed since review", code="recovery_review_changed"
        )
    codex = bridge.native_codex()
    selection = bridge.data.get("codex_last_selection") if codex else bridge.agent_selection()
    if selection is None or selection["state"] != "ready":
        raise BridgeError(
            "original native selection cannot be confirmed", code="recovery_selection_invalid"
        )
    matches, audit = bridge.selected_native_evidence(selection)
    if audit["state"] == "failed" or any(
        task["id"] not in matches for task in bridge.data["tasks"]
    ):
        raise BridgeError(
            "original native turns cannot be confirmed", code="recovery_session_invalid"
        )
    fresh = bridge.hc("get", "--json", terminal["address"])
    bridge.identity(fresh)
    if (
        fresh.get("pid") != terminal["pid"]
        or fresh.get("argv") != argv
        or fresh.get("state") != "live"
        or not fresh.get("keep")
        or (fresh.get("status") or {}).get("state") is not None
    ):
        raise BridgeError("recovered shell changed before restart", code="recovery_shell_changed")
    private_files = () if codex else ("pi-selection.json", "pi-settled.json")
    for name in private_files:
        if (bridge.root / name).is_symlink():
            raise BridgeError("native evidence path changed", code="recovery_session_invalid")
    record.update(
        state="launch_unknown",
        actor=actor,
        review=review,
        fingerprint=expected_fingerprint,
        token=secrets.token_hex(20),
        session_id=selection["session_id"],
        shell_pid=terminal["pid"],
        started_at=time.time(),
    )
    bridge.data["recovery"] = record
    bridge.persist()
    fsync_directory(bridge.root)
    for name in private_files:
        path = bridge.root / name
        path.unlink(missing_ok=True)
    command = build_agent_command(
        bridge.data,
        bridge.root,
        session_file=current["session_file"],
        session_id=selection["session_id"] if codex else None,
        startup_token=record["token"],
    )
    try:
        record["receipt"] = bridge.hc(
            "send",
            "--json",
            "--wait",
            "--timeout",
            "1",
            terminal["address"],
            shlex.join(["exec", *command]),
        )
    except (HcError, subprocess.TimeoutExpired, OSError):
        record["reason"] = "recovery_delivery_unknown"
    bridge.persist()
    return summary(record)


def confirm_startup(bridge, terminal, selection):
    record = bridge.data.get("recovery") or {}
    if (
        record.get("state") != "launch_unknown"
        or selection is None
        or selection["state"] != "ready"
    ):
        return
    if (
        selection["session_id"] != record["session_id"]
        or terminal.get("pid") != record["shell_pid"]
        or terminal.get("state") != "live"
        or (terminal.get("status") or {}).get("app") != bridge.agent()
        or (terminal.get("status") or {}).get("state") not in ("idle", "done")
    ):
        return
    if bridge.native_codex():
        foreground = terminal.get("foreground") or {}
        argv = foreground.get("argv") or []
        if (
            foreground.get("pid") != record["shell_pid"]
            or foreground.get("name") != "codex"
            or not argv
            or argv[1:2] != ["resume"]
            or codex_native.STATUS_LINE not in argv
            or argv[-1] != record["session_id"]
        ):
            return
    else:
        try:
            startup = json.loads((bridge.root / "pi-selection.json").read_text())
        except (OSError, ValueError):
            return
        if startup.get("startup_token") != record["token"]:
            return
    matches, audit = bridge.selected_native_evidence(selection, lost=True)
    if audit["state"] == "failed" or any(
        task["id"] not in matches for task in bridge.data["tasks"]
    ):
        return
    if bridge.data["tasks"]:
        task = bridge.data["tasks"][-1]
        evidence = matches[task["id"]]
        task["recovery"] = {
            "state": "restored",
            "thread_id": evidence["thread_id"],
            "agent_turn_id": evidence["agent_turn_id"],
            "basis": verification_basis(evidence),
            "aborted_message_id": evidence.get("aborted_message_id"),
        }
    record.update(state="restored", finished_at=time.time())
    record.pop("reason", None)
    bridge.data["agent_pid"] = terminal["pid"]
    bridge.data["agent_argv"] = terminal.get("argv")
