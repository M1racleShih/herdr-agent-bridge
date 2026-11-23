"""Owner-bound local CLI for Grok Bot; no approval or uncertain-delivery replay.

Use the registered local desktop, not the Bot's cloud computer. Public JSON
contains task evidence, never the native transcript, provider config or auth.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from herdr_agent_bridge import codex_native, hermes_relay, native_commands
from herdr_agent_bridge.agent_adapters import NATIVE_ENV_KEYS, PARENT_AGENT_KEYS
from herdr_agent_bridge.agent_bridge import Bridge
from herdr_agent_bridge.agent_capabilities import require_development_runtime
from herdr_agent_bridge.bridge_common import (
    PYTHON_NO_USER_SITE,
    PYTHON_USER_BASE,
    BridgeError,
    HcError,
    digest,
    hc_error_kind,
    run_hc,
    save,
)
from herdr_agent_bridge.bridge_common import fsync_directory as sync_directory
from herdr_agent_bridge.bridge_protocol import AGENTS, PI_THINKING_LEVELS, public_task
from herdr_agent_bridge.core_events import CoreEvents
from herdr_agent_bridge.grok_notifications import Outbox, load_config, validate_owner
from herdr_agent_bridge.readiness import probe
from herdr_agent_bridge.readonly_contracts import PROMPTS, check_task
from herdr_agent_bridge.runtime_paths import new_manager_run

PROJECT = Path.cwd()
OUTCOMES = {"evidence_ready", "needs_human", "agent_error", "exited", "interrupted"}
# hc 0.1.18 input.rs guards startup for 10s, but wait may report idle sooner.
# Use terminal age rather than sending early and then replaying a refused task.
STARTUP_GUARD_MS = 10_000
_FRESH_SELECTION = object()
RUN_CLOSED_RECEIPT = "run-closed.json"


class InputNotReady(BridgeError):
    pass


def owner_identity(bot_id, conversation_id):
    return validate_owner({"bot_id": bot_id, "conversation_id": conversation_id})


class Manager:
    def __init__(self, root, owner, runner=run_hc, sender=None, allow_config_replacement=False):
        self.root = Path(root).resolve()
        self.owner = owner_identity(owner.get("bot_id"), owner.get("conversation_id"))
        self.path = self.root / "manager.json"
        self.runner, self.sender = runner, sender
        self.data = json.loads(self.path.read_text())
        self.check_owner(allow_missing_config=allow_config_replacement)

    def check_owner(self, allow_missing_config=False):
        validate_owner(self.data.get("owner"))
        if self.data.get("owner") != self.owner:
            raise BridgeError("Bot/conversation binding mismatch; refusing operation")
        reference = self.data.get("webhook_config")
        pinned = self.data.get("relay_route")
        config = None
        if reference:
            if allow_missing_config and pinned is None:
                try:
                    Path(reference).lstat()
                except FileNotFoundError:
                    return
                except OSError:
                    raise BridgeError("cannot inspect existing notification config") from None
            # A readable old config is never bypassed by configuration replacement.
            # Its credential may be the malformed legacy owner itself.
            config = load_config(reference, self.owner)
        profile = config.get("profile_home") if config else None
        if pinned is None:
            if profile is not None:
                raise BridgeError("cannot add Hermes Relay to an existing run")
        elif (
            not isinstance(pinned, dict) or profile is None or pinned.get("profile_home") != profile
        ):
            raise BridgeError("Hermes Relay route changed; restore the original run configuration")
        else:
            self.relay_context()

    def relay_context(self):
        pinned = self.data.get("relay_route")
        if pinned is None:
            return None
        try:
            return hermes_relay.validate_context(
                {k: v for k, v in pinned.items() if k != "profile_home"}
            )
        except ValueError as exc:
            raise BridgeError(str(exc)) from None

    @classmethod
    def create(
        cls,
        root,
        project,
        owner,
        model=None,
        webhook_config=None,
        source_home=None,
        runner=run_hc,
        sender=None,
        agent="codex",
        development=None,
        thinking=None,
        runtime="isolated",
        relay_context=None,
    ):
        project = Path(project).resolve()
        root = new_manager_run(root, project)
        if agent not in AGENTS:
            raise BridgeError("unsupported coding agent")
        if development is not None:
            require_development_runtime(agent, runtime)
        owner = owner_identity(owner.get("bot_id"), owner.get("conversation_id"))
        if not project.is_dir():
            raise BridgeError("project directory does not exist")
        config = load_config(webhook_config, owner) if webhook_config else {}
        profile = config.get("profile_home")
        if profile is not None:
            try:
                relay_context = hermes_relay.validate_context(relay_context)
            except ValueError as exc:
                raise BridgeError(str(exc)) from None
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
        data = {
            "format": 1,
            "run_id": "grok-" + secrets.token_hex(12),
            "run": str(root.relative_to(project)),
            "owner": owner,
            "project": str(project),
            "agent": agent,
            "closed": False,
            "created_at": time.time(),
            "setup": "creation_unknown",
            "events": {},
            "verification": {},
            "webhook_config": str(Path(webhook_config).resolve()) if webhook_config else None,
            "monitor": None,
            "stop_monitor": False,
        }
        if profile is not None:
            data["relay_route"] = {"profile_home": profile, **relay_context}
        save(root / "manager.json", data)
        # Project entries can prove an empty run did not launch only if this
        # creation record and its directory are durable before backend effects.
        for directory in (root, *root.parents):
            sync_directory(directory)
            if directory == project:
                break
        manager = cls(root, owner, runner, sender)
        # Manager intent precedes Bridge creation, and Bridge intent precedes hc.
        # Repeating `start` is refused even if the first reply was lost.
        with manager.locked():
            bridge = Bridge.create(
                root / "bridge",
                project,
                model=model,
                source_home=source_home,
                runner=runner,
                agent=agent,
                development=development,
                thinking=thinking,
                runtime=runtime,
            )
            bridge.launch()
            manager.data["setup"] = "launch_attempted"
            save(manager.path, manager.data)
        return manager

    @contextmanager
    def locked(self, allow_closed=False, allow_missing_config=False):
        with (self.root / "manager.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.data = json.loads(self.path.read_text())
            self.check_owner(allow_missing_config=allow_missing_config)
            if self.data["closed"] and not allow_closed:
                raise BridgeError("manager is closed")
            if self.data.get("stop_monitor") and not allow_closed:
                raise BridgeError("manager close is in progress")
            yield

    def bridge(self):
        return Bridge(self.root / "bridge", runner=self.runner)

    def outbox(self):
        config = self.data.get("webhook_config")
        credential = load_config(config, self.owner).get("key") if config else None
        return Outbox(self.root, self.owner, credential=credential)

    def startup_remaining(self, terminal):
        created = terminal.get("created_ms")
        if type(created) is not int or created < 0:
            return None
        return max(0, (created + STARTUP_GUARD_MS - time.time() * 1000) / 1000)

    def input_ready(self, terminal):
        return (
            terminal.get("state") == "live"
            and (terminal.get("status") or {}).get("state") in ("idle", "done")
            and self.startup_remaining(terminal) == 0
        )

    def envelope(self, bridge=None, task=None, *, selection_snapshot=_FRESH_SELECTION):
        self.check_owner()
        data = bridge.data if bridge else {}
        terminal = data.get("terminal_snapshot", {})
        dev = bridge.development_summary() if bridge else None
        selection = (
            bridge.agent_selection()
            if bridge and selection_snapshot is _FRESH_SELECTION
            else selection_snapshot
            if bridge
            else None
        )
        model_selection = None
        if selection is not None:
            model_selection = {
                "state": selection["state"],
                "scope": "current_session" if bridge.native_codex() else "startup",
                "requested": {"model": data.get("model"), "thinking": data.get("thinking")},
                "actual": (
                    {
                        key: selection[key]
                        for key in ("model", "thinking", "session_id", "service_tier")
                        if key in selection
                    }
                    if selection["state"] == "ready"
                    else None
                ),
            }
            if selection["state"] == "ready":
                model_selection["native_evidence"] = selection["native_evidence"]
            if selection.get("reason"):
                model_selection["reason"] = selection["reason"]
        shown_task = public_task(task)
        if shown_task is not None and selection is not None:
            failed = selection["state"] == "failed"
            pending = (
                selection["state"] == "pending"
                or selection["state"] == "ready"
                and selection["native_evidence"] != "matched"
            )
            latest = bool(data.get("tasks")) and task["id"] == data["tasks"][-1]["id"]
            if failed or pending and latest:
                shown_task["verified_success"] = False
                shown_task["evidence"] = {key: None for key in shown_task["evidence"]}
                shown_task["diagnostic"] = {
                    "state": "failed" if failed else "pending",
                    "reason": selection.get("reason", "native_turn_incomplete"),
                }
                if shown_task["observed"] in ("evidence_ready", "needs_human"):
                    shown_task["observed"] = (
                        "needs_human"
                        if (terminal.get("status") or {}).get("state") == "blocked"
                        else "unknown"
                    )
        return {
            "format": 1,
            "run_id": self.data["run_id"],
            "run": self.data["run"],
            "agent": data.get("agent", self.data.get("agent", "codex")),
            "runtime": data.get("runtime", "isolated"),
            "owner": self.owner,
            "closed": self.data["closed"],
            "launch": data.get("launch", self.data["setup"]),
            "execution": {
                "session": data.get("session"),
                "address": data.get("address"),
                "state": terminal.get("state"),
                "status": (terminal.get("status") or {}).get("state"),
            },
            "task": shown_task,
            **(
                {"native_operation": native_commands.public_operation(operation)}
                if (operation := native_commands.latest_operation(data)) is not None
                else {}
            ),
            "model_selection": model_selection,
            **(
                {
                    "recovery": {
                        key: data["recovery"][key]
                        for key in ("state", "actor", "review", "fingerprint", "reason")
                        if key in data["recovery"]
                    }
                }
                if data.get("recovery")
                else {}
            ),
            "input_ready": (
                not self.data["closed"]
                and not self.data.get("stop_monitor")
                and self.input_ready(terminal)
                and (
                    selection is None
                    or selection["state"] == "ready"
                    and (not data["tasks"] or selection["native_evidence"] == "matched")
                )
                and native_commands.pending_control(data) is None
                and native_commands.fast_ready(data, selection)
                and (not data.get("tasks") or bridge.continuable(data["tasks"][-1]))
                and (data.get("recovery") or {}).get("state") not in ("needed", "launch_unknown")
                and (
                    not dev
                    or dev["control"] == "automation"
                    and dev["state"] == "active"
                    and (dev.get("verification_job") or {}).get("state") != "running"
                )
            ),
            **({"development": dev} if dev else {}),
            "notification_configured": bool(self.data.get("webhook_config")),
            "event_ids": list(self.data["events"].values()),
            "monitor": self.data.get("monitor"),
        }

    def status(self, task_id=None):
        with self.locked(allow_closed=True):
            if not (self.root / "bridge" / "journal.json").is_file():
                return self.envelope()
            bridge = self.bridge()
            task = bridge.task(task_id) if task_id else next(iter(bridge.data["tasks"][-1:]), None)
            return self.envelope(bridge, task)

    def configure(self, path):
        with self.locked(allow_missing_config=True):
            config = load_config(path, self.owner)
            if self.relay_context() is not None or config.get("profile_home") is not None:
                raise BridgeError("Hermes Relay is bound only at project start")
            self.data["webhook_config"] = str(Path(path).resolve())
            save(self.path, self.data)
            return {"configured": True, "owner": self.owner, "run_id": self.data["run_id"]}

    def submit(self, task_id, prompt, parent=None, development_task=None, operator=False):
        if not isinstance(task_id, str) or len(task_id) > 256:
            raise BridgeError("managed task identifier must be at most 256 characters")
        with self.locked():
            bridge = self.bridge()
            if not bridge.data["tasks"]:
                terminal = bridge.hc("get", "--json", bridge.data["terminal"])
                bridge.identity(terminal)
                if not self.input_ready(terminal):
                    raise InputNotReady(
                        "startup not ready; observe until input_ready=true; no input attempted",
                        code="native_input_not_ready",
                    )
            task = bridge.submit(
                task_id, prompt, parent, development_task=development_task, operator=operator
            )
            result = self.envelope(bridge, task)
            if operator:
                result["operator_text"] = task["submitted_text"]
            return result

    def native_command(self, operation_id, command, arguments=None, *, development_task=None):
        with self.locked():
            bridge = self.bridge()
            operation = native_commands.execute(
                bridge,
                operation_id,
                command,
                arguments,
                development_task=development_task,
                input_ready=self.input_ready,
            )
            task = next(iter(bridge.data["tasks"][-1:]), None)
            return {**self.envelope(bridge, task), "native_operation": operation}

    def development_control(self, action, **kwargs):
        with self.locked():
            bridge = self.bridge()
            if action == "takeover":
                result = bridge.takeover(kwargs["actor"])
            elif action == "resume":
                result = bridge.resume(kwargs["fingerprint"], kwargs["review"])
            elif action == "reconcile":
                bridge.observe()
                bridge.reconcile_verification()
                result = bridge.reconciliation()
            else:
                raise BridgeError("unknown development control")
            return {**self.envelope(bridge), "control_result": result}

    def interrupt(self, task_id, *, actor, note):
        with self.locked():
            bridge = self.bridge()
            bridge.interrupt(task_id, actor=actor, note=note)
            return self.envelope(bridge, bridge.task(task_id))

    def recovery_control(self, action, **kwargs):
        with self.locked():
            bridge = self.bridge()
            result = (
                bridge.recovery_status()
                if action == "recovery-status"
                else bridge.recover(
                    kwargs["fingerprint"], actor=kwargs["actor"], review=kwargs["review"]
                )
            )
            task = bridge.data["tasks"][-1] if bridge.data["tasks"] else None
            return {**self.envelope(bridge, task), "control_result": result}

    def verify_development(self, *, expected_round=None):
        from herdr_agent_bridge.development import collect, verification_lease

        bridge = self.bridge()
        with verification_lease(bridge.root) as lease:
            with self.locked():
                bridge.observe()
                ticket = bridge.begin_verification(expected_round=expected_round)
            try:
                evidence = collect(
                    self.data["project"],
                    ticket.contract,
                    lease_fd=lease.fileno(),
                    cancelled=lambda: bridge.verification_cancelled(ticket),
                )
                with self.locked(allow_closed=True):
                    evidence = bridge.finish_verification(ticket, evidence)
                    save(self.root / "development-evidence.json", evidence)
                    return {**self.envelope(bridge, bridge.task(ticket.round_id)), **evidence}
            except BaseException:
                bridge.abort_verification(ticket)
                raise

    def decide_development(self, decision, actor, note):
        with self.locked(allow_closed=decision == "terminate"):
            bridge = self.bridge()
            bridge.decide_development(decision, actor, note)
            return self.envelope(bridge)

    def queue_outcome(self, bridge, *, selection_snapshot=_FRESH_SELECTION):
        task = next(iter(bridge.data["tasks"][-1:]), None)
        terminal = bridge.data.get("terminal_snapshot", {})
        selection = (
            bridge.agent_selection()
            if selection_snapshot is _FRESH_SELECTION
            else selection_snapshot
        )
        control = native_commands.pending_control(bridge.data)
        if (bridge.data.get("recovery") or {}).get("state") in ("needed", "launch_unknown"):
            kind, task_id = "recovery_required", task["id"] if task and not control else None
        elif codex_native.trust_required(selection):
            kind, task_id = "permission_required", task["id"] if task and not control else None
        elif control is not None:
            # A native command owns this activity, including its permission
            # dialog. No readiness or result event may attribute it to a round.
            state = (terminal.get("status") or {}).get("state")
            kind = (
                "exited"
                if terminal.get("state") == "finished"
                else "permission_required"
                if control.get("reason") == "native_terminal_blocked"
                else "agent_error"
                if state == "error"
                else None
            )
            task_id = None
        elif selection is not None and selection["state"] == "failed":
            kind, task_id = "agent_error", task["id"] if task else None
        elif task:
            observed = task.get("observed")
            if observed not in OUTCOMES:
                kind = None
            else:
                result = task.get("evidence", {}).get("result") or {}
                kind = {
                    "evidence_ready": "result_available",
                    "agent_error": "agent_error",
                    "exited": "exited",
                    "interrupted": "interrupted",
                    "needs_human": "question"
                    if result.get("kind") == "question"
                    and task.get("evidence", {}).get("agent_completed")
                    else "permission_required",
                }[observed]
            task_id = task["id"]
        elif terminal.get("state") == "finished":
            kind, task_id = "exited", None
        elif (terminal.get("status") or {}).get("state") == "error":
            kind, task_id = "agent_error", None
        elif (terminal.get("status") or {}).get("state") == "blocked":
            kind, task_id = "permission_required", None
        elif (
            self.input_ready(terminal)
            and (selection is None or selection["state"] == "ready")
            and native_commands.fast_ready(bridge.data, selection)
        ):
            kind, task_id = "run_ready", None
        else:
            kind, task_id = None, None

        # Keep the original event ID for the first episode, including journals
        # created before episode tracking. Later observed failures need a new ID.
        legacy_error_id = digest(json.dumps([self.data["run_id"], task_id, "agent_error"]))[:32]
        episode_key = "task:" + task_id if task_id is not None else "run"
        episodes = self.data.get("agent_error_episodes", {})
        episode = episodes.get(episode_key)
        changed = False
        if kind == "agent_error":
            if episode is None:
                episode = {"sequence": 0, "active": True}
                self.data.setdefault("agent_error_episodes", {})[episode_key] = episode
                changed = True
            elif not episode["active"]:
                episode["sequence"] += 1
                episode["active"] = True
                changed = True
            event_id = (
                legacy_error_id
                if episode["sequence"] == 0
                else digest(
                    json.dumps([self.data["run_id"], task_id, kind, "episode", episode["sequence"]])
                )[:32]
            )
        else:
            recovered = (
                selection is None
                or selection["state"] == "ready"
                and (task is None or selection.get("native_evidence") == "matched")
            )
            if episode is not None and episode["active"] and recovered:
                episode["active"] = False
                changed = True
            elif episode is None and legacy_error_id in self.data["events"]:
                self.data.setdefault("agent_error_episodes", {})[episode_key] = {
                    "sequence": 0,
                    "active": not recovered,
                }
                changed = True
            if kind is None:
                if changed:
                    save(self.path, self.data)
                return None
            event_id = digest(json.dumps([self.data["run_id"], task_id, kind]))[:32]
        if kind == "recovery_required":
            event_id = digest(
                json.dumps(
                    [self.data["run_id"], task_id, kind, bridge.data["recovery"]["original_pid"]]
                )
            )[:32]
        if event_id not in self.data["events"]:
            # Notification is only a pointer. Fetch current evidence through this
            # CLI; do not send native logs, prompts, paths or stale verification.
            payload = {
                "event_id": event_id,
                "run_id": self.data["run_id"],
                "run": self.data["run"],
                "owner": self.owner,
                "task_id": task_id,
                "kind": kind,
            }
            self.outbox().enqueue(payload)
            self.data["events"][event_id] = event_id
            changed = True
        if changed:
            save(self.path, self.data)
        return event_id

    def observe(self, task_id=None, wait=0, complete_monitor=None):
        with self.locked():
            bridge = self.bridge()
            bridge.observe(task_id=task_id, wait=wait)
            terminal = bridge.data.get("terminal_snapshot", {})
            remaining = self.startup_remaining(terminal)
            if (
                wait
                and not bridge.data["tasks"]
                and terminal.get("state") == "live"
                and (terminal.get("status") or {}).get("state") == "idle"
                and remaining is not None
                and remaining > 0
            ):
                # No hc readiness query exists for this gate. This bounded timer
                # follows v0.1.18's startup interval, then refreshes live state.
                time.sleep(min(wait, remaining))
                bridge.observe(task_id=task_id)
            task = bridge.task(task_id) if task_id else next(iter(bridge.data["tasks"][-1:]), None)
            latest = next(iter(bridge.data["tasks"][-1:]), None)
            snapshot = getattr(bridge, "observed_selection", _FRESH_SELECTION)
            event_id = (
                self.queue_outcome(bridge, selection_snapshot=snapshot) if task is latest else None
            )
            if event_id and complete_monitor:
                monitor = self.data.get("monitor") or {}
                if monitor.get("token") == complete_monitor:
                    # Retire observation under the same lock that captured the
                    # outcome, before a callback can submit the next task.
                    monitor.update(state="finished", reason="outcome", finished_at=time.time())
                    save(self.path, self.data)
            result = self.envelope(bridge, task, selection_snapshot=snapshot)
            result["event_id"] = event_id
            return result

    def verify(self, task_id, contract):
        with self.locked():
            bridge = self.bridge()
            if bridge.data.get("development"):
                raise BridgeError(
                    "development requires final diff/check verification and operator acceptance"
                )
            task = bridge.task(task_id)
            previous = bridge.task(task["parent"]) if task.get("parent") else None
            checks = check_task(contract, task, previous, Path(self.data["project"]))
            passing = bool(checks) and all(c.get("passed") is True for c in checks)
            self.data["verification"][task_id] = {
                "contract": contract,
                "checks": checks,
                "passed": passing,
            }
            save(self.path, self.data)
            if not passing:
                return {"passed": False, "checks": checks, **self.envelope(bridge, task)}
            if contract != "question":
                bridge.verify(task_id, checks)
                task = bridge.task(task_id)
            return {"passed": True, "checks": checks, **self.envelope(bridge, task)}

    def notify(self, event_id, retry=False):
        with self.locked(allow_closed=True):
            if event_id not in self.data["events"]:
                raise BridgeError("event does not belong to this run")
            config = self.data.get("webhook_config")
            relay = self.relay_context()
            outbox = self.outbox()
            event = outbox.get(event_id)
            if relay is not None and not retry and event["state"] in ("unknown", "accepted"):
                return outbox.reconcile_relay(event_id, config, relay)
            if not config or not retry and event["state"] != "pending":
                return event
            return outbox.deliver(
                event_id, config, sender=self.sender, retry=retry, relay_context=relay
            )

    def acknowledge(self, event_id, receipt):
        with self.locked(allow_closed=True):
            if self.relay_context() is not None:
                raise BridgeError("read the original Hermes Relay receipt with notify")
            if event_id not in self.data["events"]:
                raise BridgeError("event does not belong to this run")
            return self.outbox().acknowledge(event_id, self.owner, receipt)

    def monitor(self, seconds=600, launcher=None):
        if not 0 < seconds <= 3600:
            raise BridgeError("monitor duration must be between 0 and 3600 seconds")
        with self.locked():
            previous = self.data.get("monitor")
            if previous and previous["state"] != "finished":
                raise BridgeError(
                    "monitor launch unresolved; inspect it instead of starting another"
                )
            if self.data.get("stop_monitor"):
                raise BridgeError("manager close is in progress; monitor cannot be started")
            bridge = self.bridge()
            native = bridge.data.get("runtime") == "native" and launcher is None
            # Names are limited to 32 characters. Preserve an unpredictable
            # label even if the terminal address is not returned by hc run.
            token = secrets.token_hex(10 if native else 16)
            if previous and previous.get("backend") == "hc":
                self.data.setdefault("monitor_history", []).append(previous)
            self.data["monitor"] = {
                "token": token,
                "state": "launch_unknown",
                "pid": None,
                "seconds": seconds,
                "started_at": time.time(),
            }
            if native:
                self.data["monitor"].update(
                    backend="hc", name="hab-watch-" + token, address=None, observation="events"
                )
            save(self.path, self.data)  # A lost process-creation receipt is uncertain.
            sync_directory(self.root)
            if native:
                # Core launches a program with its own environment, not its
                # client's environment. Restore the saved native selectors as
                # Bridge.launch does, without passing owner or webhook secrets.
                env = bridge.data["native_env"]
                command = [
                    "env",
                    "-u",
                    "PYTHONPATH",
                    "-u",
                    "PYTHONHOME",
                    "-u",
                    "PYTHONUSERBASE",
                    "-u",
                    "PYTHONNOUSERSITE",
                ]
                for key in (*PARENT_AGENT_KEYS, *NATIVE_ENV_KEYS):
                    if key in PARENT_AGENT_KEYS or env.get(key) is None:
                        command.extend(["-u", key])
                command.extend(
                    [
                        "PYTHONUSERBASE=" + PYTHON_USER_BASE,
                        "PYTHONNOUSERSITE=" + PYTHON_NO_USER_SITE,
                    ]
                )
                command.extend(key + "=" + value for key, value in env.items() if value is not None)
                command.extend(
                    [
                        sys.executable,
                        "-P",
                        "-u",
                        "-m",
                        "herdr_agent_bridge.managed_watch",
                        str(self.root),
                        token,
                    ]
                )
                result = bridge.hc(
                    "run",
                    "--bg",
                    "--json",
                    "--no-parent",
                    "--no-notify",
                    "--name",
                    self.data["monitor"]["name"],
                    "--label",
                    "bridge_monitor=" + token,
                    "--cwd",
                    self.data["project"],
                    *command,
                )
                address = result.get("address") if isinstance(result, dict) else None
                if not isinstance(address, str) or not address:
                    raise BridgeError(
                        "monitor launch receipt lacks a terminal address; inspect existing run"
                    )
                self.data["monitor"].update(state="starting", address=address)
                save(self.path, self.data)
                return {"run_id": self.data["run_id"], "monitor": self.data["monitor"]}
            command = [
                sys.executable,
                "-P",
                "-m",
                "herdr_agent_bridge.grok_bridge",
                "--run",
                str(self.root),
                "--bot-id",
                self.owner["bot_id"],
                "--conversation-id",
                self.owner["conversation_id"],
                "watch",
                "--seconds",
                str(seconds),
                "--monitor-token",
                token,
            ]
            fd = os.open(self.root / "monitor.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as output:
                child = (launcher or subprocess.Popen)(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env={
                        "PYTHONUSERBASE": PYTHON_USER_BASE,
                        "PYTHONNOUSERSITE": PYTHON_NO_USER_SITE,
                        **{
                            key: value
                            for key, value in os.environ.items()
                            if key
                            not in (
                                "PYTHONPATH",
                                "PYTHONHOME",
                                "PYTHONUSERBASE",
                                "PYTHONNOUSERSITE",
                            )
                        },
                    },
                )
            self.data["monitor"]["pid"] = child.pid
            save(self.path, self.data)
            return {"run_id": self.data["run_id"], "monitor": self.data["monitor"]}

    def watch(self, seconds=600, monitor_token=None, *, event_source=None):
        if not 0 < seconds <= 3600:
            raise BridgeError("watch duration must be between 0 and 3600 seconds")
        deadline, reason = time.monotonic() + seconds, "deadline"
        registered = False
        try:
            if monitor_token:
                with self.locked(allow_closed=True):
                    monitor = self.data.get("monitor") or {}
                    if monitor.get("token") != monitor_token or monitor.get("state") == "finished":
                        raise BridgeError("monitor identity mismatch or already finished")
                    registered = True
                    monitor.update(state="running", pid=os.getpid())
                    save(self.path, self.data)
            if self.bridge().data.get("runtime") == "native":
                result = self._watch_core_events(deadline, monitor_token, event_source)
                reason = result["watch_status"]
                return result
            while time.monotonic() < deadline:
                with self.locked(allow_closed=True):
                    if self.data["closed"] or self.data.get("stop_monitor"):
                        reason = "closed" if self.data["closed"] else "stop_requested"
                        return {**self.envelope(), "watch_status": reason}
                # Bounded waits let close and other calls acquire the owner lock.
                result = self.observe(
                    wait=min(10, max(1, int(deadline - time.monotonic()))),
                    complete_monitor=monitor_token,
                )
                if result.get("event_id"):
                    result["notification"] = self.notify(result["event_id"])
                    reason = "outcome"
                    return {**result, "watch_status": reason}
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            return {**self.status(), "watch_status": reason}
        except BaseException:
            reason = "error"
            raise
        finally:
            if monitor_token and registered:
                with self.locked(allow_closed=True):
                    monitor = self.data.get("monitor") or {}
                    if monitor.get("token") == monitor_token:
                        monitor.update(state="finished", reason=reason, finished_at=time.time())
                        save(self.path, self.data)

    def _watch_core_events(self, deadline, monitor_token, event_source):
        dirty, needs_evidence, source = True, False, None
        with ExitStack() as stack:
            try:
                while time.monotonic() < deadline:
                    with self.locked(allow_closed=True):
                        if self.data["closed"] or self.data.get("stop_monitor"):
                            reason = "closed" if self.data["closed"] else "stop_requested"
                            return {**self.envelope(), "watch_status": reason}
                    if dirty:
                        try:
                            result = self.observe(complete_monitor=monitor_token)
                        except (HcError, OSError, subprocess.TimeoutExpired) as exc:
                            if isinstance(exc, HcError) and hc_error_kind(exc) == "not_found":
                                raise BridgeError(
                                    "observed terminal was removed", code="events_terminal_missing"
                                ) from None
                            needs_evidence = True
                        else:
                            if result.get("event_id"):
                                result["notification"] = self.notify(result["event_id"])
                                return {
                                    **result,
                                    "watch_status": "outcome",
                                    "subscription": dict(source.stats)
                                    if source
                                    else {"state": "not_needed"},
                                }
                            # Native evidence can settle just after the terminal status event.
                            needs_evidence = result["execution"]["status"] in ("idle", "done")
                    if source is None:
                        bridge = self.bridge()
                        address = bridge.data.get("address")
                        if not isinstance(address, str) or not address:
                            raise BridgeError(
                                "observe the original launch before subscribing",
                                code="events_target_unknown",
                            )
                        source = stack.enter_context(
                            (event_source or CoreEvents)(address, bridge.env())
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    changed = source.read(min(1 if needs_evidence else 10, remaining))
                    dirty = changed or needs_evidence
                    if monitor_token:
                        self._monitor_update(monitor_token, subscription=dict(source.stats))
                return {
                    **self.status(),
                    "watch_status": "deadline",
                    "subscription": dict(source.stats) if source else {"state": "not_needed"},
                }
            finally:
                stack.close()
                if source is not None and monitor_token:
                    self._monitor_update(monitor_token, subscription=dict(source.stats))

    def _monitor_update(self, token, **changes):
        """Update this monitor even if a later round has replaced it."""
        with self.locked(allow_closed=True):
            monitors = [*self.data.get("monitor_history", []), self.data.get("monitor") or {}]
            monitor = next((item for item in monitors if item.get("token") == token), None)
            if monitor is None:
                raise BridgeError("monitor record disappeared; refusing cleanup")
            monitor.update(changes)
            save(self.path, self.data)
            sync_directory(self.root)
            return dict(monitor)

    @staticmethod
    def _monitor_terminal(bridge, monitor):
        token, name, address = monitor.get("token"), monitor.get("name"), monitor.get("address")
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[0-9a-f]{20}", token)
            or name != "hab-watch-" + token
            or address is not None
            and (not isinstance(address, str) or not address)
        ):
            raise BridgeError("invalid monitor identity; refusing cleanup")
        try:
            terminal = bridge.hc("get", "--json", address or name, timeout=10)
        except HcError as exc:
            if hc_error_kind(exc) == "not_found":
                return None
            raise
        if (
            not isinstance(terminal, dict)
            or not isinstance(terminal.get("address"), str)
            or not terminal["address"]
            or terminal.get("name") != name
            or (address and terminal["address"] != address)
            or not isinstance(terminal.get("labels"), dict)
            or terminal["labels"].get("bridge_monitor") != token
        ):
            raise BridgeError("monitor terminal identity changed; refusing cleanup")
        return terminal

    def _cleanup_monitor(self, bridge, monitor):
        """Clean only the recorded watcher, reconciling uncertain mutations."""
        token = monitor["token"]
        cleanup = dict(monitor.get("cleanup") or {})
        if cleanup.get("state") == "complete":
            return {"passed": True, "name": monitor["name"]}

        def update(**changes):
            nonlocal monitor, cleanup
            cleanup.update(changes)
            monitor = self._monitor_update(token, cleanup=cleanup)

        def unresolved(reason):
            update(state="unresolved", reason=reason)
            return {"passed": False, "name": monitor.get("name"), "reason": reason}

        try:
            terminal = self._monitor_terminal(bridge, monitor)
            if terminal is None:
                # A timed-out run may still be created after this lookup.
                if monitor.get("address") is None and monitor.get("state") == "launch_unknown":
                    return unresolved("launch outcome not yet observed")
                update(state="complete", reason="absent")
                return {"passed": True, "name": monitor["name"]}
            if monitor.get("address") is None:
                monitor = self._monitor_update(token, address=terminal["address"])

            if cleanup.get("kill") == "unknown":
                if terminal.get("state") != "finished":
                    return unresolved("kill outcome not yet observed")
                update(kill="complete")
            elif cleanup.get("kill") != "complete":
                if terminal.get("state") == "live":
                    update(state="in_progress", kill="unknown", reason=None)
                    try:
                        bridge.hc("kill", monitor["address"], timeout=10)
                    except (HcError, subprocess.TimeoutExpired, OSError):
                        terminal = self._monitor_terminal(bridge, monitor)
                        if terminal is not None and terminal.get("state") != "finished":
                            return unresolved("kill outcome not yet observed")
                        if terminal is None:
                            update(state="complete", reason="absent")
                            return {"passed": True, "name": monitor["name"]}
                    update(kill="complete")
                elif terminal.get("state") == "finished":
                    update(kill="complete")
                else:
                    return unresolved("unexpected terminal state")

            # Validate the immutable address and random label again before rm.
            terminal = self._monitor_terminal(bridge, monitor)
            if terminal is None:
                update(state="complete", reason="absent")
                return {"passed": True, "name": monitor["name"]}
            if cleanup.get("rm") == "unknown":
                return unresolved("remove outcome not yet observed")
            if terminal.get("state") != "finished":
                return unresolved("terminal has not exited")
            update(state="in_progress", rm="unknown", reason=None)
            try:
                bridge.hc("rm", monitor["address"], timeout=10)
            except (HcError, subprocess.TimeoutExpired, OSError):
                if self._monitor_terminal(bridge, monitor) is not None:
                    return unresolved("remove outcome not yet observed")
            update(state="complete", rm="complete", reason="removed")
            return {"passed": True, "name": monitor["name"]}
        except (BridgeError, subprocess.TimeoutExpired, OSError):
            return unresolved("monitor identity or terminal state could not be verified")

    def close(self):
        # Serialize closers, but never hold manager.lock while waiting for a
        # watcher to exit: it may itself be acquiring that lock to finish.
        with (self.root / "manager-close.lock").open("a") as close_lock:
            fcntl.flock(close_lock, fcntl.LOCK_EX)
            with self.locked(allow_closed=True):
                self.data["stop_monitor"] = True
                save(self.path, self.data)
                monitors = [*self.data.get("monitor_history", []), self.data.get("monitor") or {}]
                monitors = [item for item in monitors if item.get("backend") == "hc"]
            monitor_cleanup = []
            if (self.root / "bridge" / "journal.json").is_file():
                bridge = self.bridge()
                for monitor in monitors:
                    monitor_cleanup.append(self._cleanup_monitor(bridge, monitor))
            with self.locked(allow_closed=True):
                if not (self.root / "bridge" / "journal.json").is_file():
                    self.data["closed"] = True
                    save(self.path, self.data)
                    return {**self.envelope(), "cleanup": []}
                bridge = self.bridge()
                cleanup = bridge.close()
                self.data["closed"] = bridge.data["closed"] and all(
                    item["passed"] for item in monitor_cleanup
                )
                save(self.path, self.data)
                if (
                    self.data["closed"] is True
                    and isinstance(self.data.get("run"), str)
                    and isinstance(self.data.get("project"), str)
                    and bridge.data.get("project") == self.data["project"]
                ):
                    # Use the existing close verdict, but give admission a small
                    # durable record that does not include task data.
                    sync_directory(bridge.root)
                    save(
                        self.root / RUN_CLOSED_RECEIPT,
                        {"format": 1, "project": self.data["project"], "run": self.data["run"]},
                    )
                    sync_directory(self.root)
                result = {
                    **self.envelope(bridge),
                    "cleanup": [
                        {"operation": item.get("args", ["unknown"])[0], "passed": item["passed"]}
                        for item in cleanup
                    ],
                }
                if monitor_cleanup:
                    result["monitor_cleanup"] = monitor_cleanup
                return result


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--bot-id")
    parser.add_argument("--conversation-id")
    parser.add_argument("--expect-run-id")
    commands = parser.add_subparsers(dest="command", required=True)
    ready = commands.add_parser("probe")
    ready.add_argument("--project", type=Path, default=PROJECT)
    ready.add_argument("--agent", choices=AGENTS, default="codex")
    prompt = commands.add_parser("prompt")
    prompt.add_argument("contract", choices=tuple(PROMPTS))
    start = commands.add_parser("start")
    start.add_argument("--project", type=Path, default=PROJECT)
    start.add_argument("--agent", choices=AGENTS, default="codex")
    start.add_argument(
        "--model", help="Codex model, or pi provider/model (default: saved pi selection)"
    )
    start.add_argument("--thinking", choices=PI_THINKING_LEVELS, help="explicit Pi thinking level")
    start.add_argument("--webhook-config", type=Path)
    start.add_argument("--development-contract", type=Path)
    submit = commands.add_parser("submit")
    submit.add_argument("task_id")
    inputs = submit.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--prompt-file", type=Path)
    inputs.add_argument("--contract", choices=tuple(PROMPTS))
    submit.add_argument("--parent")
    submit.add_argument("--monitor", action="store_true")
    submit.add_argument("--development-task")
    submit.add_argument(
        "--operator", action="store_true", help="prepare exact App input without sending it"
    )
    native = commands.add_parser("native-command")
    native.add_argument("operation_id")
    native.add_argument("slash_command")
    native.add_argument("--arguments")
    native.add_argument("--development-task")
    observe = commands.add_parser("observe")
    observe.add_argument("--task-id")
    observe.add_argument("--wait", type=int, default=0)
    status = commands.add_parser("status")
    status.add_argument("--task-id")
    verify = commands.add_parser("verify")
    verify.add_argument("task_id")
    verify.add_argument("--contract", required=True, choices=tuple(PROMPTS))
    config = commands.add_parser("configure")
    config.add_argument("--webhook-config", required=True, type=Path)
    notify = commands.add_parser("notify")
    notify.add_argument("event_id")
    notify.add_argument("--retry", action="store_true")
    acknowledge = commands.add_parser("ack")
    acknowledge.add_argument("event_id")
    acknowledge.add_argument("--receipt", required=True)
    monitor = commands.add_parser("monitor")
    monitor.add_argument("--seconds", type=int, default=600)
    watch = commands.add_parser("watch")
    watch.add_argument("--seconds", type=int, default=600)
    watch.add_argument("--monitor-token", help=argparse.SUPPRESS)
    commands.add_parser("close")
    commands.add_parser("verify-development")
    stop = commands.add_parser("interrupt")
    stop.add_argument("task_id")
    stop.add_argument("--actor", required=True)
    stop.add_argument("--note", required=True)
    commands.add_parser("recovery-status")
    recover = commands.add_parser("recover")
    recover.add_argument("--fingerprint", required=True)
    recover.add_argument("--actor", required=True)
    recover.add_argument("--review", required=True)
    commands.add_parser("reconcile")
    takeover = commands.add_parser("takeover")
    takeover.add_argument("--actor", required=True)
    resume = commands.add_parser("resume")
    resume.add_argument("--fingerprint", required=True)
    resume.add_argument("--review", required=True)
    decision = commands.add_parser("decide")
    decision.add_argument("decision", choices=("accept", "reject", "terminate"))
    decision.add_argument("--actor", required=True)
    decision.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    if args.command == "probe":
        result = probe(args.project, agent=args.agent)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1
    if args.command == "prompt":
        print(PROMPTS[args.contract])
        return 0
    if not args.run or not args.bot_id or not args.conversation_id:
        parser.error("--run, --bot-id and --conversation-id are required for managed operations")
    try:
        owner = owner_identity(args.bot_id, args.conversation_id)
        if args.command == "start":
            manager = Manager.create(
                args.run,
                args.project,
                owner,
                args.model,
                args.webhook_config,
                agent=args.agent,
                development=json.loads(args.development_contract.read_text())
                if args.development_contract
                else None,
                thinking=args.thinking,
            )
            result = manager.status()
        else:
            manager = Manager(args.run, owner, allow_config_replacement=args.command == "configure")
            if args.expect_run_id and manager.data["run_id"] != args.expect_run_id:
                raise BridgeError("run identity mismatch")
            if args.command == "submit":
                text = PROMPTS[args.contract] if args.contract else args.prompt_file.read_text()
                result = manager.submit(
                    args.task_id,
                    text,
                    args.parent,
                    development_task=args.development_task,
                    operator=args.operator,
                )
                if args.monitor:
                    try:
                        result.update(manager.monitor())
                    except (BridgeError, OSError):
                        # The task was already submitted. Preserve its receipt;
                        # failure to start observation is a separate condition.
                        result["monitor_error"] = (
                            "observer launch unresolved; inspect this run and observe the existing task"
                        )
            elif args.command == "native-command":
                result = manager.native_command(
                    args.operation_id,
                    args.slash_command,
                    args.arguments,
                    development_task=args.development_task,
                )
            elif args.command in ("status", "observe"):
                result = (
                    manager.observe(args.task_id, args.wait)
                    if args.command == "observe"
                    else manager.status(args.task_id)
                )
            elif args.command == "verify":
                result = manager.verify(args.task_id, args.contract)
            elif args.command == "verify-development":
                result = manager.verify_development()
            elif args.command == "interrupt":
                result = manager.interrupt(args.task_id, actor=args.actor, note=args.note)
            elif args.command in ("recovery-status", "recover"):
                result = manager.recovery_control(
                    args.command,
                    **{
                        key: getattr(args, key)
                        for key in ("fingerprint", "actor", "review")
                        if hasattr(args, key)
                    },
                )
            elif args.command in ("takeover", "resume", "reconcile"):
                result = manager.development_control(
                    args.command,
                    **{
                        k: v
                        for k, v in vars(args).items()
                        if k in ("actor", "fingerprint", "review")
                    },
                )
            elif args.command == "decide":
                result = manager.decide_development(args.decision, args.actor, args.note)
            elif args.command == "configure":
                result = manager.configure(args.webhook_config)
            elif args.command == "notify":
                result = manager.notify(args.event_id, args.retry)
            elif args.command == "ack":
                result = manager.acknowledge(args.event_id, args.receipt)
            elif args.command == "monitor":
                result = manager.monitor(args.seconds)
            elif args.command == "watch":
                result = manager.watch(args.seconds, args.monitor_token)
            else:
                result = manager.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("passed") is False else 0
    except (BridgeError, OSError, ValueError, KeyboardInterrupt) as exc:
        # HcError includes native command/output. Keep those in private artifacts.
        error = (
            "hc_call_failed; inspect private run artifacts"
            if isinstance(exc, HcError)
            else type(exc).__name__
        )
        detail = str(exc) if isinstance(exc, BridgeError) and not isinstance(exc, HcError) else None
        print(
            json.dumps(
                {
                    "error": error,
                    "delivery": "not_attempted" if isinstance(exc, InputNotReady) else "unknown",
                    "detail": detail,
                    "action": "inspect the existing run; do not replay start or submit",
                }
            ),
            file=sys.stderr,
        )
        return 1


def main(argv=None):
    """Preserve cancellation cleanup for every public and compatibility entry."""

    def interrupted(signum, frame):
        raise KeyboardInterrupt("interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return _main(argv)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
