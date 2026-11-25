"""Small local, single-writer bridge for one interactive coding agent in hc.

No notification transport, automatic approval, replay, or task-success inference.
Native Codex/Pi histories are version-specific evidence, not an API contract.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from herdr_agent_bridge import codex_native, native_commands
from herdr_agent_bridge.agent_adapters import (
    NATIVE_ENV_KEYS,
    PARENT_AGENT_KEYS,
    build_agent_command,
    extract_turns,
    prepare_isolated_agent,
    validate_agent_selection,
)
from herdr_agent_bridge.agent_capabilities import require_development_runtime
from herdr_agent_bridge.bridge_common import (
    BridgeError,
    HcError,
    digest,
    fsync_directory,
    hc_error_kind,
    run_hc,
    save,
)
from herdr_agent_bridge.bridge_protocol import (
    AGENTS,
    CODEX_THINKING_LEVELS,
    DEVELOPMENT_MODEL,
    PI_THINKING_LEVELS,
    decode_result,
)
from herdr_agent_bridge.bridge_protocol import verification_basis as _verification_basis
from herdr_agent_bridge.codex_evidence import extract_path as extract_codex_path
from herdr_agent_bridge.development import VerificationBusy, verification_lease
from herdr_agent_bridge.development_task import DevelopmentTask
from herdr_agent_bridge.pi_evidence import extract_selected_turns
from herdr_agent_bridge.pi_evidence import extract_turns as extract_pi_turns
from herdr_agent_bridge.terminal_recovery import confirm_startup, observe_process, review_snapshot
from herdr_agent_bridge.terminal_recovery import start as start_recovery

_LEGACY_NATIVE_ERRORS = {
    "native_session_mismatch",
    "native_selection_mismatch",
    "native_selection_unavailable",
    "native_dispatch_mismatch",
}


class Bridge:
    def __init__(self, root, runner=run_hc):
        self.root = Path(root).resolve()
        self.runner = runner
        self.path = self.root / "journal.json"
        self.data = json.loads(self.path.read_text())

    @classmethod
    def create(
        cls,
        root,
        project,
        model=None,
        source_home=None,
        runner=run_hc,
        agent="codex",
        development=None,
        thinking=None,
        runtime="isolated",
    ):
        if runtime not in ("isolated", "native"):
            raise BridgeError("unsupported runtime")
        if runtime == "native" and source_home is not None:
            raise BridgeError(
                "native runtime uses the normal agent home; source_home is for isolated tests"
            )
        validate_agent_selection(agent, model, thinking, runtime, project=project)
        if development is None:
            return cls._create(
                root, project, model, source_home, runner, agent, development, thinking, runtime
            )
        require_development_runtime(agent, runtime)
        if agent == "pi":
            from herdr_agent_bridge.bridge_protocol import policy

            selected = policy({"model": model or DEVELOPMENT_MODEL, "thinking": thinking or "high"})
            model, thinking = selected["model"], selected["thinking"]
        project = Path(project).resolve()
        if not project.is_dir() or Path(root).exists():
            raise BridgeError("development requires an existing project and a new run directory")
        guard = project / ".artifacts" / "development-active.json"
        guard.parent.mkdir(parents=True, exist_ok=True)
        with guard.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if guard.exists():
                previous = json.loads(guard.read_text())
                journal = Path(previous["journal"])
                if not journal.is_file():
                    raise BridgeError(
                        "previous development creation unresolved; inspect before starting"
                    )
                data = json.loads(journal.read_text())
                if (data.get("development") or {}).get("state") not in ("accepted", "terminated"):
                    raise BridgeError(
                        "one active development task per project; reconcile the original run"
                    )
                if not data.get("closed"):
                    raise BridgeError(
                        "clean up the previous development run before starting another"
                    )
            # Validate before reserving; preserve unknown creation rather than replaying it.
            from herdr_agent_bridge.development import prepare

            prepare(project, development)
            save(guard, {"journal": str(Path(root).resolve() / "journal.json")})
            return cls._create(
                root, project, model, source_home, runner, agent, development, thinking, runtime
            )

    @classmethod
    def _create(
        cls,
        root,
        project,
        model=None,
        source_home=None,
        runner=run_hc,
        agent="codex",
        development=None,
        thinking=None,
        runtime="isolated",
    ):
        root, project = Path(root).resolve(), Path(project).resolve()
        if agent not in AGENTS:
            raise BridgeError("unsupported coding agent")
        if agent == "codex" and runtime == "isolated" and model is None:
            model = "gpt-6.1-terra"
        if not project.is_dir():
            raise BridgeError("project directory does not exist")
        contract = None
        if development is not None:
            from herdr_agent_bridge.development import prepare

            contract = prepare(project, development)
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
        if runtime == "native":
            scratch = root / "native"
            (scratch / agent / "sessions").mkdir(parents=True, mode=0o700)
        else:
            scratch = Path(tempfile.mkdtemp(prefix="hab-agent-", dir="/tmp"))
            for name in ("config", "state", "run", "cache"):
                (scratch / name).mkdir(mode=0o700)
            hc_config = scratch / "config" / "herdr-core"
            hc_config.mkdir(mode=0o700)
            (hc_config / "config.toml").write_text("notes = false\n")
        data = {
            "format": 1,
            "session": "hab-" + secrets.token_hex(8),
            "scratch": str(scratch),
            "project": str(project),
            "terminal": "agent",
            "address": None,
            "agent": agent,
            "model": model,
            "launch": "not_submitted",
            "tasks": [],
            "closed": False,
            "created_at": time.time(),
        }
        data["runtime"] = runtime
        if runtime == "native":
            data["terminal"] = data["session"]
            # Save only environment selectors, never credentials or whole environments.
            data["native_env"] = {key: os.environ.get(key) for key in NATIVE_ENV_KEYS}
        if thinking is not None:
            data["thinking"] = thinking
        if contract is not None:
            data["development"] = DevelopmentTask.create(contract).to_record()
        save(root / "journal.json", data)  # Recovery coordinates precede any side effect.
        obj = cls(root, runner)
        try:
            prepare_isolated_agent(data, root, source_home)
        except BaseException:
            shutil.rmtree(scratch)
            data["closed"] = True
            data["launch"] = "configuration_failed"
            save(root / "journal.json", data)
            raise
        obj.data = data
        return obj

    def agent(self):
        # Existing format-1 journals predate the explicit agent field.
        agent = self.data.get("agent", "codex")
        if agent not in AGENTS:
            raise BridgeError("unsupported coding agent in journal")
        return agent

    def env(self):
        if self.data.get("runtime") == "native":
            env = dict(os.environ)
            for key, value in self.data["native_env"].items():
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = value
            for key in PARENT_AGENT_KEYS:
                env.pop(key, None)
            return env
        parent_agent_vars = {
            "CODEX_SESSION_ID",
            "CODEX_THREAD_ID",
            "CODEX_TOOL_CALL_ID",
            "CODEX_CI",
            "PI_SESSION_ID",
            "PI_SESSION_FILE",
            "PI_PROVIDER",
            "PI_MODEL",
            "PI_REASONING_LEVEL",
            "PI_CODING_AGENT",
            "AI_AGENT",
        }
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("HC_", "PI_")) and k not in parent_agent_vars
        }
        scratch = Path(self.data["scratch"])
        for key, folder in [
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_STATE_HOME", "state"),
            ("XDG_RUNTIME_DIR", "run"),
            ("XDG_CACHE_HOME", "cache"),
        ]:
            env[key] = str(scratch / folder)
        env["HC_SESSION"] = self.data["session"]
        env["CODEX_HOME"] = str(scratch / "codex")
        env["PI_CODING_AGENT_DIR"] = str(scratch / "pi")
        env["PI_CODING_AGENT_SESSION_DIR"] = str(scratch / "pi" / "sessions")
        if self.agent() == "pi":
            for key in (
                "NODE_OPTIONS",
                "NODE_PATH",
                "NODE_REPL_EXTERNAL_MODULE",
                "NODE_V8_COVERAGE",
            ):
                env.pop(key, None)
            env.update(
                PI_PROGRAM_STATUS="1", PI_SKIP_VERSION_CHECK="1", PI_OFFLINE="1", PI_TELEMETRY="0"
            )
        return env

    @contextmanager
    def locked(self, allow_closed=False):
        with (self.root / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.data = json.loads(self.path.read_text())
            if self.data["closed"] and not allow_closed:
                raise BridgeError("session is closed")
            yield

    def persist(self):
        save(self.path, self.data)

    def native_codex(self):
        return self.agent() == "codex" and self.data.get("runtime") == "native"

    def agent_selection(self, *, terminal=None, screen=None):
        """Inspect live Codex evidence under the lock, or return the saved observation."""
        if not self.native_codex():
            return self.pi_selection()
        if terminal is None:
            return self.data.get("selection_snapshot") or {"state": "pending"}
        result = codex_native.inspect(self, terminal, screen)
        if result["state"] == "ready":
            try:
                _, audit = self.selected_native_evidence(result)
            except BridgeError as exc:
                audit = {"state": "failed", "reason": exc.code}
            if audit["state"] == "failed":
                result = audit
            else:
                result["native_evidence"] = audit["state"]
                if audit.get("reason"):
                    result["reason"] = audit["reason"]
                self.data["codex_last_selection"] = result
        self.data["selection_snapshot"] = result
        return result

    def native_history(self):
        if self.native_codex():
            return codex_native.history(self.data)
        directory = Path(self.data["scratch"]) / self.agent() / "sessions"
        paths = list(directory.rglob("*.jsonl"))
        if (
            directory.is_symlink()
            or len(paths) != 1
            or paths[0].is_symlink()
            or not paths[0].is_file()
            or not paths[0].resolve().is_relative_to(directory.resolve())
        ):
            return None
        return paths[0]

    def selected_native_evidence(self, selection, *, lost=False):
        if not self.native_codex():
            return extract_selected_turns(
                Path(self.data["scratch"]) / "pi", self.data["tasks"], selection
            )
        path = self.native_history()
        if path is None:
            return {}, {"state": "pending", "reason": "codex_rollout_pending"}
        return extract_codex_path(
            path,
            self.data["tasks"],
            thread_id=selection["session_id"],
            project=self.data["project"],
            lost=lost,
        )

    def pi_selection(self):
        """Read the SDK's initial selection evidence; absent legacy data stays compatible."""
        if self.agent() != "pi" or (
            self.data.get("thinking") is None and self.data.get("runtime") != "native"
        ):
            return None
        if self.data.get("closed") and "selection_snapshot" in self.data:
            return self.data["selection_snapshot"]
        legacy_error = self.data.get("selection_error")
        if legacy_error and (
            not isinstance(legacy_error, str)
            or legacy_error not in _LEGACY_NATIVE_ERRORS
            or not self.data["tasks"]
        ):
            return {"state": "failed", "reason": legacy_error}
        path = self.root / "pi-selection.json"
        try:
            if path.is_symlink() or (
                path.exists() and (not path.is_file() or path.stat().st_size > 4096)
            ):
                return {"state": "failed", "reason": "invalid_evidence"}
            selection = json.loads(path.read_text())
        except FileNotFoundError:
            return {"state": "pending"}
        except (OSError, ValueError):
            return {"state": "failed", "reason": "invalid_evidence"}
        if not isinstance(selection, dict):
            return {"state": "failed", "reason": "invalid_evidence"}
        if selection.get("state") == "failed" and selection.get("reason") in (
            "model_unavailable",
            "thinking_unsupported",
            "selection_mismatch",
            "selection_validation_unavailable",
            "startup_failed",
        ):
            return {"state": "failed", "reason": selection["reason"]}
        session_id = selection.get("session_id")
        if (
            selection.get("state") != "ready"
            or self.data.get("model") is not None
            and selection.get("model") != self.data["model"]
            or self.data.get("thinking") is not None
            and selection.get("thinking") != self.data["thinking"]
            or not isinstance(selection.get("model"), str)
            or "/" not in selection["model"]
            or selection.get("thinking") not in PI_THINKING_LEVELS
            or not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 256
            or any(ord(c) < 32 for c in session_id)
        ):
            return {"state": "failed", "reason": "invalid_evidence"}
        if self.data["tasks"] and self.data["tasks"][0].get("selection_session_id") != session_id:
            return {"state": "failed", "reason": "selection_mismatch"}
        result = {
            "state": "ready",
            "model": selection["model"],
            "thinking": selection["thinking"],
            "session_id": session_id,
            "native_evidence": "pending",
        }
        if self.data["tasks"]:
            _, audit = extract_selected_turns(
                Path(self.data["scratch"]) / "pi", self.data["tasks"], result
            )
            if audit["state"] == "failed":
                return audit
            result["native_evidence"] = audit["state"]
            if audit.get("reason"):
                result["reason"] = audit["reason"]
        return result

    def hc(self, *args, timeout=20):
        return self.runner(list(args), self.env(), timeout)

    def launch(self):
        with self.locked():
            if self.data["launch"] != "not_submitted":
                raise BridgeError(
                    "launch already attempted; inspect the existing terminal, never replay"
                )
            self.data["launch"] = "delivery_unknown"
            self.persist()
            try:
                command = build_agent_command(self.data, self.root)
                result = self.hc(
                    "run",
                    "--bg",
                    "--json",
                    "--no-parent",
                    "--no-notify",
                    *(["--keep"] if self.data.get("runtime") == "native" else []),
                    "--name",
                    self.data["terminal"],
                    "--label",
                    "bridge_run=" + self.data["session"],
                    "--cwd",
                    self.data["project"],
                    *command,
                )
            except (HcError, subprocess.TimeoutExpired, OSError) as exc:
                self.data["launch_error"] = str(exc)
                self.persist()
                raise
            self.data["launch"] = "submitted"
            self.data["address"] = result["address"]
            self.data["launch_receipt"] = result
            self.persist()
            return result

    def identity(self, terminal):
        if self.data.get("address") and terminal["address"] != self.data["address"]:
            raise BridgeError("terminal identity changed; refusing input, correlation or cleanup")
        if not self.data.get("address") or self.data.get("runtime") == "native":
            if terminal.get("labels", {}).get("bridge_run") != self.data["session"]:
                raise BridgeError("launch identity cannot be reconciled")
            self.data["address"] = terminal["address"]

    def target(self):
        return self.data.get("address") or self.data["terminal"]

    def settled(self, evidence, aborted=False):
        if self.data.get("runtime") != "native" or self.native_codex():
            return True
        path = self.root / "pi-settled.json"
        try:
            if path.is_symlink() or path.stat().st_size > 4096:
                return False
            record = json.loads(path.read_text())
            message_id = evidence.get("aborted_message_id" if aborted else "final_message_id")
            if (
                record.get("state") == "starting"
                and record.get("session_id") == evidence.get("thread_id")
                and (self.data.get("recovery") or {}).get("state") == "restored"
                and self.data["tasks"]
            ):
                recovery = self.data["tasks"][-1].get("recovery") or {}
                if (
                    recovery.get("state") == "restored"
                    and recovery.get("thread_id") == evidence.get("thread_id")
                    and recovery.get("agent_turn_id") == evidence.get("agent_turn_id")
                ):
                    return (
                        bool(message_id) and recovery.get("aborted_message_id") == message_id
                        if aborted
                        else recovery.get("basis") is not None
                        and recovery["basis"] == _verification_basis(evidence)
                    )
            return (
                record.get("state") == "settled"
                and record.get("aborted") is aborted
                and record.get("session_id") == evidence.get("thread_id")
                and record.get("aborted_message_id" if aborted else "final_message_id")
                == message_id
                and bool(message_id)
            )
        except (OSError, ValueError, AttributeError):
            return False

    def task(self, task_id):
        for task in self.data["tasks"]:
            if task["id"] == task_id:
                return task
        raise BridgeError("unknown task id")

    def development(self):
        record = self.data.get("development")
        return DevelopmentTask(record) if record is not None else None

    def development_summary(self):
        task = self.development()
        return task.summary([item["id"] for item in self.data["tasks"]]) if task else None

    @staticmethod
    def continuable(task):
        evidence = task.get("evidence") or {}
        return task.get("delivery") == "received" and (
            (
                task.get("observed") in ("evidence_ready", "needs_human")
                and evidence.get("agent_completed")
                and evidence.get("result")
            )
            or (
                task.get("observed") == "interrupted"
                and evidence.get("agent_aborted")
                and (task.get("interrupt") or {}).get("state") == "stopped"
            )
            or (
                task.get("observed") == "interrupted"
                and evidence.get("agent_lost")
                and (task.get("recovery") or {}).get("state") == "restored"
            )
        )

    def recovery_status(self):
        with self.locked():
            terminal = self.hc("get", "--json", self.target())
            self.identity(terminal)
            observe_process(self, terminal)
            recovery = self.data.get("recovery") or {"state": "healthy"}
            review = review_snapshot(self, terminal)
            self.persist()
            return {
                **{key: recovery[key] for key in ("state", "actor", "review") if key in recovery},
                "fingerprint": review["fingerprint"],
                "files": review["files"],
            }

    def recover(self, fingerprint, *, actor, review):
        with self.locked():
            terminal = self.hc("get", "--json", self.target())
            self.identity(terminal)
            observe_process(self, terminal)
            dev = self.development()
            if dev:
                dev.assert_input(dev.contract["task_id"], False)
            result = start_recovery(self, terminal, fingerprint, actor, review)
        self.observe()
        from herdr_agent_bridge.terminal_recovery import summary

        return summary(self.data.get("recovery") or result)

    def interrupt(self, task_id, *, actor, note):
        if (
            not isinstance(actor, str)
            or not actor.strip()
            or len(actor) > 256
            or not isinstance(note, str)
            or not note.strip()
            or len(note) > 4096
        ):
            raise BridgeError("interrupt requires an actor and a note", code="interrupt_invalid")
        with self.locked():
            task = self.task(task_id)
            if task.get("interrupt"):
                return {key: value for key, value in task["interrupt"].items() if key != "receipt"}
            if task is not self.data["tasks"][-1]:
                raise BridgeError(
                    "only the latest round can be interrupted", code="interrupt_not_current"
                )
            dev = self.development()
            if dev:
                dev.assert_input(dev.contract["task_id"], False)
            terminal = self.hc("get", "--json", self.target())
            self.identity(terminal)
            if self.native_codex():
                observe_process(self, terminal)
                if (self.data.get("recovery") or {}).get("state") in ("needed", "launch_unknown"):
                    self.persist()
                    raise BridgeError(
                        "reconcile the lost agent before interrupting", code="recovery_required"
                    )
                selection = self.agent_selection(terminal=terminal)
                if selection["state"] != "ready":
                    raise BridgeError(
                        "bound Codex thread cannot be confirmed",
                        code=selection.get("reason", "codex_binding_pending"),
                    )
            if (
                terminal["state"] != "live"
                or (terminal.get("status") or {}).get("state") != "working"
            ):
                raise BridgeError(
                    "interrupt requires a working agent", code="interrupt_not_working"
                )
            record = {
                "state": "unknown",
                "actor": actor,
                "note": note,
                "requested_at": time.time(),
                "before_turn": terminal["turn"],
            }
            task["interrupt"] = record
            task["verified_success"] = False
            if dev:
                dev.invalidate()
            self.persist()
            fsync_directory(self.root)
            try:
                record["receipt"] = self.hc(
                    "interrupt", "--json", "--timeout", "10", terminal["address"]
                )
            except (HcError, subprocess.TimeoutExpired, OSError):
                record["reason"] = "interrupt_delivery_unknown"
            self.persist()
        self.observe(task_id)
        return {
            key: value for key, value in self.task(task_id)["interrupt"].items() if key != "receipt"
        }

    def submit(self, task_id, prompt, parent=None, development_task=None, operator=False):
        with self.locked():
            dev = self.development()
            if dev:
                dev.assert_input(development_task, operator)
            elif development_task is not None or operator:
                raise BridgeError("not a development run")
            if any(t["id"] == task_id for t in self.data["tasks"]):
                raise BridgeError("task id already exists; observe it instead of replaying")
            if (
                not task_id
                or not task_id.isascii()
                or not all(c.isalnum() or c in "-_" for c in task_id)
            ):
                raise BridgeError(
                    "task id must contain ASCII letters, digits, hyphen or underscore"
                )
            if self.data["launch"] not in ("submitted", "observed"):
                raise BridgeError("launch not reconciled")
            terminal = self.hc("get", "--json", self.target())
            self.identity(terminal)
            observe_process(self, terminal)
            self.persist()
            if (self.data.get("recovery") or {}).get("state") in ("needed", "launch_unknown"):
                raise BridgeError(
                    "reconcile the lost agent before sending input", code="recovery_required"
                )
            selection = self.agent_selection(terminal=terminal)
            self.persist()
            if selection is not None and selection["state"] != "ready":
                raise BridgeError(
                    self.agent().capitalize()
                    + " selection "
                    + selection["state"]
                    + ": "
                    + selection.get("reason", "await native confirmation"),
                    code="native_input_not_ready",
                )
            if (
                selection is not None
                and self.data["tasks"]
                and selection["native_evidence"] != "matched"
            ):
                raise BridgeError(
                    "native selection pending: observe the original session",
                    code="native_input_not_ready",
                )
            state = (terminal.get("status") or {}).get("state")
            if terminal["state"] != "live" or state not in ("idle", "done"):
                raise BridgeError(
                    "not ready for ordinary input: " + str(state), code="native_input_not_ready"
                )
            if self.data["tasks"]:
                last = self.data["tasks"][-1]
                if not self.continuable(last):
                    raise BridgeError(
                        "previous task unresolved; observe it, do not submit another prompt"
                    )
                if parent != last["id"]:
                    raise BridgeError("follow-up must name the latest task as parent")
            elif parent:
                raise BridgeError("first task cannot have a parent")
            native_commands.assert_ordinary_input(self, terminal, selection=selection)
            if self.native_codex():
                fresh = self.hc("get", "--json", terminal["address"])
                self.identity(fresh)
                if any(
                    fresh.get(key) != terminal.get(key)
                    for key in ("pid", "turn", "status", "state", "foreground")
                ):
                    raise BridgeError(
                        "Codex terminal changed before input", code="codex_terminal_changed"
                    )
            wrapped = (
                f"BRIDGE_TASK_ID={task_id}\nRead-only task. Do not change files, start subagents, "
                "or approve permission/trust dialogs. Return your final answer as a JSON object "
                f'with task_id="{task_id}", kind="result" or "question", and answer (an object). '
                "Use kind=question if you need a human decision.\n\n" + prompt
            )
            if dev:
                contract = dev.contract
                wrapped = (
                    f"BRIDGE_TASK_ID={task_id}\nDevelopment task {development_task}; "
                    "this is one round of the SAME task. Do not approve permission/trust "
                    "dialogs, merge or deploy. "
                    + (
                        "Use your normal tools, extensions and skills for this task. "
                        if self.data.get("runtime") == "native"
                        else "Use scoped_edit/scoped_write and run_check for changes and checks. "
                    )
                    + f"Goal: {contract['goal']}\nAllowed files: {json.dumps(contract['allowed_paths'], ensure_ascii=False)}\n"
                    f"Acceptance: {json.dumps(contract['acceptance'], ensure_ascii=False)}\n"
                    f"Checks: {json.dumps(contract['checks'], ensure_ascii=False)}\n"
                    f'Return final JSON with task_id="{task_id}", kind="result" or "question", '
                    "and answer (an object). Ask a question for an unresolved decision.\n\n"
                    + prompt
                )
            task = {
                "id": task_id,
                "parent": parent,
                "submitted_text": wrapped,
                "input_sha256": digest(wrapped),
                "before_turn": terminal["turn"],
                "delivery": "unknown",
                "observed": "unknown",
                "verified_success": False,
            }
            if selection is not None:
                task["selection_session_id"] = selection["session_id"]
                if self.native_codex():
                    task["codex_selection"] = {
                        key: selection[key] for key in ("model", "thinking", "service_tier")
                    }
            self.data["tasks"].append(task)
            if dev:
                task["development_task"] = development_task
                task["input_source"] = "operator-ui" if operator else "manager"
                dev.invalidate()
            self.persist()  # Intent is durable before invoking hc; crash => unknown, not retryable.
            if operator:
                return dict(task)  # Operator types this exact prepared text in the App.
            try:
                receipt = self.hc(
                    "send", "--json", "--wait", "--timeout", "1", terminal["address"], wrapped
                )
                task["receipt"] = receipt
                task["delivery"] = "submitted"
            except (HcError, subprocess.TimeoutExpired, OSError) as exc:
                task["delivery_error"] = str(exc)
                # Even an error reply may follow a side effect: never infer non-delivery.
            self.persist()
            return dict(task)

    def takeover(self, actor):
        with self.locked():
            dev = self.development()
            if not dev:
                raise BridgeError("active development task and named operator required")
            terminal = self.hc("get", "--json", self.target())
            self.identity(terminal)
            dev.takeover(actor, terminal["turn"])
            self.persist()
            return {"control": "operator", "terminal": terminal["address"]}

    def begin_verification(self, *, expected_round=None):
        """Reserve one verification; never hold the journal lock during checks."""
        from herdr_agent_bridge.development import fingerprint, snapshot

        with self.locked():
            dev = self.development()
            if not dev or not self.data["tasks"]:
                raise BridgeError("active development task required")
            if native_commands.pending_control(self.data):
                raise BridgeError("native command requires reconciliation before verification")
            latest = self.data["tasks"][-1]
            if native_commands.goal_follows(self.data, latest["id"]):
                raise BridgeError(
                    "inspect changes from the native goal before verifying a new round"
                )
            if expected_round is not None and latest["id"] != expected_round:
                raise BridgeError(
                    "event round is no longer current; do not verify a different round",
                    code="verification_round_not_current",
                )
            native = self.reconciliation()["sessions"]
            if self.native_codex() and not self.codex_snapshot_ready(native):
                raise BridgeError("bound Codex history cannot be verified")
            ticket = dev.begin_verification(
                latest,
                fingerprint(snapshot(self.data["project"], dev.contract["allowed_paths"])),
                native,
            )
            self.persist()
            fsync_directory(self.root)
            return ticket

    def finish_verification(self, ticket, evidence):
        from herdr_agent_bridge.development import fingerprint, snapshot

        with self.locked(allow_closed=True):
            dev = self.development()
            evidence = dev.finish_verification(
                ticket,
                evidence,
                fingerprint(snapshot(self.data["project"], dev.contract["allowed_paths"])),
                self.reconciliation()["sessions"],
                self.data["tasks"][-1]["id"],
                closed=self.data["closed"],
            )
            self.persist()
            return evidence

    def verification_cancelled(self, ticket):
        with self.locked(allow_closed=True):
            return self.development().verification_cancelled(ticket, closed=self.data["closed"])

    def abort_verification(self, ticket):
        with self.locked(allow_closed=True):
            self.development().abort_verification(ticket.token)
            self.persist()

    def reconcile_verification(self):
        """Resolve a stopped verifier only after its process lease is free."""
        with self.locked():
            dev = self.development()
            if dev and dev.verifying:
                try:
                    with verification_lease(self.root):
                        dev.interrupt_verification()
                        self.persist()
                except VerificationBusy:
                    pass  # A live parent or inherited child still owns the check.

    def decide_development(self, decision, actor, note):
        from herdr_agent_bridge.development import fingerprint, snapshot

        # Closing resources is not a development decision. An operator can
        # end the task after cleanup without reopening the run.
        with self.locked(allow_closed=decision == "terminate"):
            dev = self.development()
            if not dev:
                raise BridgeError("explicit operator decision required")
            current = (
                fingerprint(snapshot(self.data["project"], dev.contract["allowed_paths"]))
                if decision == "accept"
                else None
            )
            native = self.reconciliation()["sessions"] if decision == "accept" else None
            dev.decide(decision, actor, note, worktree_sha256=current, native=native)
            self.persist()

    def codex_snapshot_ready(self, sessions):
        return bool(sessions.get((self.data.get("codex_binding") or {}).get("session_id")))

    def reconciliation(self, terminal=None):
        from herdr_agent_bridge.development import fingerprint, snapshot

        dev = self.development()
        if not dev:
            raise BridgeError("not a development run")
        files = snapshot(self.data["project"], dev.contract["allowed_paths"])
        if self.native_codex():
            sessions = {}
            try:
                terminal = terminal or self.hc("get", "--json", self.target())
                self.identity(terminal)
                if (
                    self.data.get("codex_binding")
                    and (selection := codex_native.inspect(self, terminal))["state"] == "ready"
                ):
                    history = self.native_history()
                    _, audit = self.selected_native_evidence(selection)
                    if history is not None and audit["state"] == "matched":
                        sessions[selection["session_id"]] = digest(
                            history.read_text(encoding="utf-8")
                        )
                    elif history is None and not self.data["tasks"]:
                        sessions[selection["session_id"]] = digest("")
            except (BridgeError, OSError, ValueError, subprocess.TimeoutExpired):
                # A missing or changed native source supersedes an earlier review.
                pass
        else:
            native = Path(self.data["scratch"]) / self.agent() / "sessions"
            sessions = {
                str(p.relative_to(native)): digest(p.read_text()) for p in native.rglob("*.jsonl")
            }
        return {
            "fingerprint": fingerprint({"files": files, "sessions": sessions}),
            "files": files,
            "sessions": sessions,
        }

    def resume(self, expected_fingerprint, review):
        with self.locked():
            dev = self.development()
            if not dev:
                raise BridgeError("operator review required before resuming")
            terminal = self.hc("get", "--json", self.target())
            self.identity(terminal)
            if self.native_codex():
                observe_process(self, terminal)
                if (self.data.get("recovery") or {}).get("state") in ("needed", "launch_unknown"):
                    self.persist()
                    raise BridgeError(
                        "reconcile the lost agent before handing back control",
                        code="recovery_required",
                    )
            if terminal["state"] != "live" or (terminal.get("status") or {}).get("state") not in (
                "idle",
                "done",
            ):
                raise BridgeError("terminal is not ready for recovery")
            if self.data["tasks"]:
                latest = self.data["tasks"][-1]
                if latest.get("observed") not in (
                    "evidence_ready",
                    "needs_human",
                ) and not self.continuable(latest):
                    raise BridgeError("observe and reconcile the original round before resuming")
            if native_commands.pending_control(self.data):
                raise BridgeError(
                    "native command requires reconciliation before handing back control"
                )
            current = self.reconciliation(terminal)
            if self.native_codex() and not self.codex_snapshot_ready(current["sessions"]):
                raise BridgeError("bound Codex history cannot be confirmed")
            if current["fingerprint"] != expected_fingerprint:
                raise BridgeError("worktree/session changed since operator review")
            dev.resume(review, expected_fingerprint, terminal["turn"])
            self.persist()
            return {"control": "automation", "fingerprint": expected_fingerprint}

    def observe(self, task_id=None, wait=0):
        with self.locked():
            if wait:
                if not 0 < wait <= 30:
                    raise BridgeError("wait must be at most 30 seconds")
                if self.data.get("runtime") == "native":
                    self.identity(self.hc("get", "--json", self.target()))
                self.data["last_wait"] = self.hc(
                    "wait", "--json", "--timeout", str(wait), self.target(), timeout=wait + 10
                )
            terminal = self.hc("get", "--json", self.target())
            self.identity(terminal)
            observe_process(self, terminal)
            self.data["launch"] = "observed"
            self.data["terminal_snapshot"] = terminal
            agent = self.agent()
            screen = None
            if agent == "codex":
                screen = self.hc("read", "--json", "--lines", "0", self.target())
            selection = self.agent_selection(terminal=terminal, screen=screen)
            restoring = (self.data.get("recovery") or {}).get("state") == "launch_unknown"
            confirm_startup(self, terminal, selection)
            if restoring and (self.data.get("recovery") or {}).get("state") == "restored":
                selection = self.agent_selection(terminal=terminal, screen=screen)
            pending_control = native_commands.observe_controls(self, terminal, selection=selection)
            if selection is None:
                extractor = extract_turns if agent == "codex" else extract_pi_turns
                matches = extractor(Path(self.data["scratch"]) / agent, self.data["tasks"])
            elif selection["state"] == "ready" and self.data["tasks"]:
                matches, audit = self.selected_native_evidence(selection)
                if audit["state"] == "failed":
                    matches = {}
                    selection = audit
                else:
                    selection = {**selection, "native_evidence": audit["state"]}
                    if audit.get("reason"):
                        selection["reason"] = audit["reason"]
                    else:
                        selection.pop("reason", None)
                    if audit["state"] == "pending":
                        latest_id = self.data["tasks"][-1]["id"] if self.data["tasks"] else None
                        matches = {
                            key: value
                            for key, value in matches.items()
                            if key != latest_id
                            and (
                                value.get("agent_completed") is True
                                or value.get("agent_aborted") is True
                                or value.get("agent_lost") is True
                            )
                        }
                    elif (
                        isinstance(self.data.get("selection_error"), str)
                        and self.data["selection_error"] in _LEGACY_NATIVE_ERRORS
                    ):
                        self.data.pop("selection_error")
            elif (
                self.native_codex()
                and selection["state"] == "pending"
                and self.data.get("codex_last_selection")
            ):
                # Retain earlier results when a native panel temporarily hides the footer.
                try:
                    matches, _ = self.selected_native_evidence(self.data["codex_last_selection"])
                except BridgeError:
                    matches = {}
            else:
                matches = {}
            if self.native_codex() and selection["state"] == "failed":
                dev = self.development()
                if dev:
                    dev.request_cancellation()
                    dev.invalidate()
            if self.native_codex():
                self.data["selection_snapshot"] = selection
            self.observed_selection = selection
            for task in self.data["tasks"]:
                evidence = matches.get(task["id"])
                previous_state = task.get("observed")
                previous_basis = _verification_basis(task.get("evidence"))
                if task.get("verified_success"):
                    expected = task.get("verification_basis")
                    if expected is None or _verification_basis(evidence) != expected:
                        task["verified_success"] = False
                        task.pop("verification", None)
                        task.pop("verification_basis", None)
                    else:
                        task["verification_basis"] = expected
                task["observed"] = "unknown"
                if selection is not None and evidence is None:
                    task.pop("evidence", None)
                    task["diagnostic"] = {
                        "state": ("failed" if selection["state"] == "failed" else "pending"),
                        "reason": selection.get("reason", "native_turn_incomplete"),
                    }
                else:
                    task.pop("diagnostic", None)
                state = (terminal.get("status") or {}).get("state")
                if evidence:
                    task["delivery"] = "received"
                    task["evidence"] = evidence
                    try:
                        evidence["result"] = decode_result(evidence.get("answer") or "", task["id"])
                    except (json.JSONDecodeError, ValueError):
                        evidence.pop("result", None)
                if evidence and evidence.get("agent_lost"):
                    task["observed"] = "interrupted"
                elif (
                    task is not self.data["tasks"][-1]
                    and evidence
                    and evidence.get("agent_aborted")
                    and (task.get("interrupt") or {}).get("state") == "stopped"
                ):
                    task["observed"] = "interrupted"
                elif terminal["state"] != "live":
                    task["observed"] = "exited"
                elif (
                    (
                        state in ("working", "idle", "done")
                        and task is not self.data["tasks"][-1]
                        or self.native_codex()
                        and selection["state"] == "pending"
                        or pending_control
                    )
                    and previous_basis is not None
                    and previous_basis == _verification_basis(evidence)
                    and evidence.get("result")
                    and previous_state
                    == (
                        "needs_human"
                        if evidence["result"]["kind"] == "question"
                        else "evidence_ready"
                    )
                ):
                    task["observed"] = previous_state
                elif state == "blocked":
                    task["observed"] = "needs_human"
                elif state == "error":
                    task["observed"] = "agent_error"
                elif state == "working":
                    task["observed"] = "working"
                elif (
                    evidence
                    and evidence.get("agent_aborted")
                    and task.get("interrupt")
                    and terminal["turn"] > task["before_turn"]
                    and state in ("idle", "done")
                    and (task is not self.data["tasks"][-1] or self.settled(evidence, aborted=True))
                ):
                    task["interrupt"]["state"] = "stopped"
                    task["interrupt"].pop("reason", None)
                    task["observed"] = "interrupted"
                elif (
                    evidence
                    and evidence.get("agent_completed")
                    and (selection is None or selection["state"] == "ready")
                    and evidence.get("result")
                    and self.settled(evidence)
                    and terminal["turn"] > task["before_turn"]
                    and state in ("idle", "done")
                ):
                    task["observed"] = (
                        "needs_human"
                        if evidence["result"]["kind"] == "question"
                        else "evidence_ready"
                    )
            if screen is None:
                screen = self.hc(
                    "read", "--json", "--lines", "0" if agent == "codex" else "200", self.target()
                )
            save(self.root / "terminal.json", screen)
            self.persist()
            return dict(self.task(task_id)) if task_id else terminal

    def verify(self, task_id, checks):
        """Only caller-supplied independent checks may mark success; status cannot."""
        with self.locked():
            if self.data.get("development"):
                raise BridgeError(
                    "development needs final-worktree verification and explicit acceptance"
                )
            task = self.task(task_id)
            if task.get("observed") != "evidence_ready":
                raise BridgeError("no correlated, completed result available")
            if not checks or not all(
                isinstance(c, dict) and c.get("passed") is True and c.get("check") for c in checks
            ):
                raise BridgeError("independent checks must all pass")
            basis = _verification_basis(task.get("evidence"))
            if basis is None:
                raise BridgeError("no correlated, completed result available")
            task["verification"] = checks
            task["verification_basis"] = basis
            task["verified_success"] = True
            self.persist()

    def close(self):
        with self.locked(allow_closed=True):
            if self.data["closed"]:
                return self.data.get("cleanup", [])
            dev = self.development()
            if dev and dev.verifying:
                dev.request_cancellation()
                self.persist()
            try:
                with verification_lease(self.root):
                    if dev and dev.verifying:
                        dev.interrupt_verification()
                        self.persist()
                    return self._close_resources()
            except VerificationBusy:
                return [{"args": ["verify"], "passed": False, "reason": "verification_stopping"}]

    def _close_resources(self):
        """Caller holds the journal lock and the exclusive verification lease."""
        cleanup = []
        target = self.data.get("address") or self.data["terminal"]
        try:
            terminal = self.hc("get", "--json", target)
        except HcError as exc:
            if hc_error_kind(exc) != "not_found":
                raise
            terminal = None  # Confirmed absent, not just a timeout.
        if terminal is not None:
            self.identity(terminal)
            for command in ("kill", "rm"):
                args = (command, terminal["address"])
                record = {"args": args, "passed": False}
                cleanup.append(record)
                self.data["cleanup"] = cleanup
                self.persist()
                try:
                    record["result"] = self.hc(*args)
                    record["passed"] = True
                except (HcError, subprocess.TimeoutExpired, OSError) as exc:
                    record["error"] = str(exc)
                    # Reconcile a possibly applied mutation before any repeat.
                    try:
                        observed = self.hc("get", "--json", terminal["address"])
                        record["passed"] = command == "kill" and observed.get("state") == "finished"
                    except HcError as observed_error:
                        record["passed"] = hc_error_kind(observed_error) == "not_found"
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                self.persist()
                if not record["passed"]:
                    return cleanup
        else:
            cleanup.append({"args": ["get", target], "passed": True, "reconciled": "absent"})
        if self.data.get("runtime") == "native":
            # Shared core/App and native homes belong to the user. Retain our evidence.
            self.data["cleanup"] = cleanup
            self.data["selection_snapshot"] = self.agent_selection()
            self.data["closed"] = True
            self.persist()
            return cleanup
        # This core was created with our private random session + XDG paths.
        # Do not stop even that core if an unexpected live terminal appeared.
        listing = self.hc("ls", "--all", "--json")
        terminals = listing.get("terminals") if isinstance(listing, dict) else None
        if not isinstance(terminals, list) or any(
            not isinstance(t, dict) or t.get("state") != "finished" for t in terminals
        ):
            cleanup.append(
                {
                    "args": ["server", "stop"],
                    "passed": False,
                    "error": "unexpected live terminal in private core; inspect before cleanup",
                }
            )
        else:
            try:
                result = self.hc("server", "stop")
                cleanup.append({"args": ["server", "stop"], "passed": True, "result": result})
            except (HcError, subprocess.TimeoutExpired, OSError) as exc:
                cleanup.append({"args": ["server", "stop"], "passed": False, "error": str(exc)})
        self.data["cleanup"] = cleanup
        if all(c["passed"] for c in cleanup):
            selection = self.agent_selection()
            if selection is not None:
                self.data["selection_snapshot"] = selection
                self.persist()  # Preserve validated evidence before removing the native session.
            shutil.rmtree(self.data["scratch"])
            self.data["closed"] = True
        self.persist()
        return cleanup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path, help="private artifact directory")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--project", required=True, type=Path)
    start.add_argument("--agent", choices=AGENTS, default="codex")
    start.add_argument("--runtime", choices=("isolated", "native"), default="isolated")
    start.add_argument(
        "--model", help="Codex model, or pi provider/model (default: saved pi selection)"
    )
    start.add_argument(
        "--thinking", choices=sorted(set(PI_THINKING_LEVELS + CODEX_THINKING_LEVELS))
    )
    send = sub.add_parser("submit")
    send.add_argument("task_id")
    send.add_argument("--prompt-file", required=True, type=Path)
    send.add_argument("--parent")
    observe = sub.add_parser("observe")
    observe.add_argument("--task-id")
    observe.add_argument("--wait", type=int, default=0)
    stop = sub.add_parser("interrupt")
    stop.add_argument("task_id")
    stop.add_argument("--actor", required=True)
    stop.add_argument("--note", required=True)
    sub.add_parser("recovery-status")
    recover = sub.add_parser("recover")
    recover.add_argument("--fingerprint", required=True)
    recover.add_argument("--actor", required=True)
    recover.add_argument("--review", required=True)
    sub.add_parser("close")
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            bridge = Bridge.create(
                args.run,
                args.project,
                args.model,
                agent=args.agent,
                thinking=args.thinking,
                runtime=args.runtime,
            )
            result = bridge.launch()
        else:
            bridge = Bridge(args.run)
            if args.command == "submit":
                result = bridge.submit(args.task_id, args.prompt_file.read_text(), args.parent)
            elif args.command == "observe":
                result = bridge.observe(args.task_id, args.wait)
            elif args.command == "interrupt":
                result = bridge.interrupt(args.task_id, actor=args.actor, note=args.note)
            elif args.command == "recovery-status":
                result = bridge.recovery_status()
            elif args.command == "recover":
                result = bridge.recover(args.fingerprint, actor=args.actor, review=args.review)
            else:
                result = bridge.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (BridgeError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
