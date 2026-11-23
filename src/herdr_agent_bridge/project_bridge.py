"""Persistent private project entry around the owner-bound Manager.

Request intents are durable before creation. An unresolved creation is inspected,
never replayed. This file contains configuration and run bindings, not task state.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

from herdr_agent_bridge.agent_adapters import validate_agent_selection
from herdr_agent_bridge.agent_capabilities import require_development_runtime
from herdr_agent_bridge.bridge_common import BridgeError, HcError, fsync_directory, run_hc, save
from herdr_agent_bridge.bridge_protocol import (
    AGENTS,
    DEVELOPMENT_MODEL,
    PI_THINKING_LEVELS,
    PUBLIC_FIELDS,
    ExecutionPolicy,
    policy,
    selection_agent,
)
from herdr_agent_bridge.grok_bridge import RUN_CLOSED_RECEIPT, Manager
from herdr_agent_bridge.grok_notifications import (
    _credential_free,
    _no_duplicate_keys,
    load_config,
    validate_owner,
)
from herdr_agent_bridge.runtime_paths import RUNS, valid_run_pointer

DEFAULTS = {"model": DEVELOPMENT_MODEL, "thinking": "high"}
AGENT_DEFAULTS = {"pi": DEFAULTS, "codex": {"model": "gpt-6.1-terra", "thinking": "max"}}
THINKING = PI_THINKING_LEVELS
SHARED_START_REFUSALS = {
    "shared_start_required",
    "project_start_conflict",
    "project_admission_unresolved",
    "project_start_abandoned",
}
START_REFUSALS = SHARED_START_REFUSALS | {"execution_policy_invalid", "project_start_mismatch"}
INPUT_REFUSALS = {"input_owner_mismatch", "verification_in_progress"}
SUBMIT_REFUSALS = INPUT_REFUSALS | {
    "codex_terminal_changed",
    "native_control_pending",
    "native_fast_required",
    "native_input_not_ready",
    "recovery_required",
}
NATIVE_COMMAND_REFUSALS = INPUT_REFUSALS | {"native_command_invalid", "native_operation_conflict"}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise BridgeError("identifier must contain 1–100 letters, digits, underscores or hyphens")
    return value


def app_space_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
        raise BridgeError("App space id must contain 1–200 safe identifier characters")
    return value


def checks_config(value):
    if not isinstance(value, list):
        raise BridgeError("project checks must be a list")
    names = set()
    for check in value:
        if (
            not isinstance(check, dict)
            or set(check) != {"name", "argv", "timeout"}
            or not isinstance(check.get("name"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", check["name"])
            or check["name"] in names
            or not isinstance(check["argv"], list)
            or not check["argv"]
            or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in check["argv"])
            or type(check["timeout"]) is not int
            or not 1 <= check["timeout"] <= 120
        ):
            raise BridgeError("invalid project check; require unique name, argv and timeout 1–120")
        names.add(check["name"])
    return json.loads(json.dumps(value))


def reject_constant(value):
    raise ValueError("non-JSON constant")


def input_json(path, limit=65_536):
    """Bound callback/contract input before parsing untrusted JSON."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError()
        return json.loads(raw, object_pairs_hook=_no_duplicate_keys, parse_constant=reject_constant)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise BridgeError("cannot read valid bounded input JSON") from None


def private_json(path, *, max_bytes=4_194_304):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise BridgeError("project config must be a private regular file with mode 0600")
            raw = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
        if max_bytes is not None and len(raw) > max_bytes:
            raise ValueError()
        return json.loads(raw, object_pairs_hook=_no_duplicate_keys, parse_constant=reject_constant)
    except BridgeError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise BridgeError("cannot read valid private project config") from None


class Project:
    def __init__(
        self, config, *, runner=run_hc, sender=None, expected_binding=None, expected_project_id=None
    ):
        self.path = Path(config).absolute()
        self.runner, self.sender = runner, sender
        self.expected_binding = expected_binding
        self.expected_project_id = expected_project_id
        self.data = private_json(self.path)
        self.validate()

    @classmethod
    def initialize(
        cls,
        config,
        project_id,
        project_root,
        owner,
        *,
        webhook_config=None,
        defaults=None,
        agent="pi",
        checks=None,
        runtime="native",
        app_space=None,
    ):
        path, root = Path(config).absolute(), Path(project_root).resolve()
        identifier(project_id)
        if runtime not in ("native", "isolated"):
            raise BridgeError("unsupported runtime")
        agent = selection_agent(agent)
        owner = validate_owner(owner)
        if not root.is_dir():
            raise BridgeError("project root must be an existing directory")
        reference = str(Path(webhook_config).absolute()) if webhook_config else None
        credential = load_config(reference, owner).get("key") if reference else None
        data = {
            "format": 1,
            "project_id": project_id,
            "project_root": str(root),
            "owner": owner,
            "webhook_config": reference,
            "agent": agent,
            "defaults": policy(
                AGENT_DEFAULTS[agent] if defaults is None else defaults, agent=agent
            ),
            "checks": checks_config([] if checks is None else checks),
            "starts": {},
            "active_request": None,
            "runtime": runtime,
        }
        if app_space is not None:
            data["app_space"] = app_space_id(app_space)
        _credential_free(data, credential)
        _credential_free(list(PUBLIC_FIELDS), credential)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Serialize initialize and later updates on the same private lock.
        with cls.file_lock(path):
            if path.exists() or path.is_symlink():
                raise BridgeError("project config already exists")
            cls.persist(path, data)
        return cls(path)

    @staticmethod
    @contextmanager
    def file_lock(path):
        fd = os.open(
            path.with_name(path.name + ".lock"),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        with os.fdopen(fd, "a+") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise BridgeError("project lock must be a regular file")
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    @classmethod
    @contextmanager
    def _admission_lock(cls, root):
        artifacts = Path(root) / ".artifacts"
        artifacts.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(artifacts.lstat().st_mode):
            raise BridgeError("project artifacts must be a real directory")
        directory = artifacts / "project-admission"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
        ):
            raise BridgeError("project admission must be a private directory")
        path = directory / "slot.json"
        with cls.file_lock(path):
            yield path

    @staticmethod
    def _binding_path(path):
        # Normalize parent aliases, but never accept a symlink as the config itself.
        try:
            return path.parent.resolve() / path.name
        except (OSError, RuntimeError):
            raise BridgeError("cannot resolve project config parent") from None

    @staticmethod
    def _admission_state(path, root):
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        data = private_json(path)
        if (
            not isinstance(data, dict)
            or data.keys() != {"format", "project_root", "active"}
            or type(data["format"]) is not int
            or data["format"] != 1
            or data["project_root"] != root
        ):
            raise BridgeError("invalid shared project slot")
        slot = data["active"]
        if slot is not None:
            if (
                not isinstance(slot, dict)
                or slot.keys() != {"config", "request_id", "run"}
                or not isinstance(slot["config"], str)
                or not Path(slot["config"]).is_absolute()
                or not valid_run_pointer(slot["run"], legacy=True, project_entry=True)
            ):
                raise BridgeError("invalid shared project slot")
            identifier(slot["request_id"])
        return data

    def _slot(self, request_id, record):
        return {
            "config": str(self._binding_path(self.path)),
            "request_id": request_id,
            "run": record["run"],
        }

    def _slot_run_closed(self, slot):
        root = self.run_path(slot["run"])
        bridge = root / "bridge"
        if bridge.is_symlink():
            raise BridgeError("invalid shared project run")
        project = self.data["project_root"]
        receipt = root / RUN_CLOSED_RECEIPT
        try:
            receipt.lstat()
        except FileNotFoundError:
            pass
        else:
            data = private_json(receipt, max_bytes=16_384)
            if (
                not isinstance(data, dict)
                or type(data.get("format")) is not int
                or data != {"format": 1, "project": project, "run": slot["run"]}
            ):
                raise BridgeError("invalid shared project close receipt")
            return True

        # Older runs have no receipt. Runtime records do not have the config
        # size limit: a development journal can include original file contents.
        manager, journal = (
            private_json(root / "manager.json", max_bytes=None),
            private_json(bridge / "journal.json", max_bytes=None),
        )
        if (
            not isinstance(manager, dict)
            or not isinstance(journal, dict)
            or manager.get("run") != slot["run"]
            or manager.get("project") != project
            or journal.get("project") != project
            or type(manager.get("closed")) is not bool
            or type(journal.get("closed")) is not bool
        ):
            raise BridgeError("invalid shared project run")
        return manager["closed"] and journal["closed"]

    @classmethod
    def enable_shared_starts(cls, *configs):
        """Adopt existing occupancy once, from the complete trusted legacy inventory."""
        if not configs:
            raise BridgeError("shared starts require project configs")
        paths = sorted({cls._binding_path(Path(config).absolute()) for config in configs})
        root = cls(paths[0]).data["project_root"]
        with cls._admission_lock(root) as path:
            if cls._admission_state(path, root) is None:
                active = None
                for config in paths:
                    entry = cls(config)
                    if entry.data["project_root"] != root:
                        raise BridgeError("shared starts require the same canonical project root")
                    if entry.data["active_request"] is None:
                        continue
                    request_id, record = entry.record()
                    candidate = entry._slot(request_id, record)
                    try:
                        closed = entry._slot_run_closed(candidate)
                    except (OSError, ValueError, RuntimeError):
                        closed = False  # Unresolved legacy intents still occupy the slot.
                    if not closed:
                        if active is not None:
                            raise BridgeError(
                                "multiple legacy runs need reconciliation before adoption"
                            )
                        active = candidate
                cls.persist(path, {"format": 1, "project_root": root, "active": active})
            else:
                fsync_directory(path.parent)
            fsync_directory(path.parent.parent)
            fsync_directory(path.parent.parent.parent)

    def _check_shared_start(self, state):
        if state is None or state["active"] is None:
            return
        try:
            closed = self._slot_run_closed(state["active"])
        except (OSError, ValueError, RuntimeError):
            raise BridgeError(
                "the shared run could not be verified; reconcile before starting",
                code="project_admission_unresolved",
            ) from None
        if not closed:
            raise BridgeError(
                "a project entry holds the active run; close it before starting",
                code="project_start_conflict",
            )

    def _blocked_before_bridge(self, record, run, entries):
        """Recognize an initial Manager intent that could not enter Bridge creation."""
        if (
            record["request"]["development"] is None
            or "manager.json" not in entries
            or entries - {"manager.json", "manager.lock"}
        ):
            return False
        manager = private_json(run / "manager.json")
        if not isinstance(manager, dict) or not (
            type(manager.get("format")) is int
            and manager["format"] == 1
            and manager.get("project") == self.data["project_root"]
            and manager.get("run") == record["run"]
            and manager.get("owner") == self.data["owner"]
            and manager.get("agent") == record["request"].get("agent", "pi")
            and type(manager.get("created_at")) in (int, float)
            and manager.get("webhook_config", False) == self.data["webhook_config"]
            and manager.get("setup") == "creation_unknown"
            and manager.get("closed") is False
            and manager.get("stop_monitor") is False
            and manager.get("monitor", False) is None
            and manager.get("events") == {}
            and manager.get("verification") == {}
        ):
            return False
        guard = Path(self.data["project_root"]) / ".artifacts" / "development-active.json"
        if not guard.exists():
            return False
        reserved = private_json(guard)
        # Bridge.create checks this guard before replacing it or creating its
        # journal. A closed but still active original prevents the new launch.
        for previous in self.data["starts"].values():
            if previous["run_id"] is None:
                continue
            journal = self.run_path(previous["run"]) / "bridge" / "journal.json"
            if reserved == {"journal": str(journal)}:
                original = self.manager(previous)
                bridge = original.bridge().data
                return (
                    original.data["closed"] is True
                    and type(original.data.get("created_at")) in (int, float)
                    and original.data["created_at"] < manager["created_at"]
                    and bridge["closed"] is True
                    and bridge["project"] == self.data["project_root"]
                    and (bridge.get("development") or {}).get("state") == "active"
                )
        return False

    def abandon_start(self, request_id):
        """Operator-only: record a cancelled intent when launch provably did not begin."""
        root = self.data["project_root"]
        with self._admission_lock(root) as path, self.locked():
            if self.data["project_root"] != root:
                raise BridgeError("project root changed; reopen the project entry")
            state = self._admission_state(path, root)
            request_id, record = self.record(identifier(request_id))
            slot = self._slot(request_id, record)
            run = self.run_path(record["run"])
            if record["run_id"] is not None:
                raise BridgeError("start has a bound run; inspect and close the original run")
            if record.get("admission") != "abandoned":
                try:
                    info = run.lstat()
                except FileNotFoundError:
                    entries = set()
                else:
                    if not stat.S_ISDIR(info.st_mode):
                        raise BridgeError("invalid run directory; inspect the original run")
                    entries = {child.name for child in run.iterdir()}
                if entries and not self._blocked_before_bridge(record, run, entries):
                    raise BridgeError("run has creation evidence; inspect the original run")
                if not entries and (state is None or record.get("admission") != "shared"):
                    raise BridgeError("this start has no recoverable shared admission record")
                record["admission"] = "abandoned"
                if self.data["active_request"] == request_id:
                    self.data["active_request"] = None
                self.persist(self.path, self.data)
            if state is not None and state["active"] == slot:
                state["active"] = None
                self.persist(path, state)
            return self.public(
                {"request_id": request_id, "abandoned": True, "delivery": "not_attempted"}
            )

    @staticmethod
    def persist(path, data):
        save(path, data)
        fsync_directory(path.parent)

    def validate(self):
        data = self.data
        if (
            not isinstance(data, dict)
            or data.get("format") != 1
            or set(data) - {"runtime", "app_space", "agent"}
            != {
                "format",
                "project_id",
                "project_root",
                "owner",
                "webhook_config",
                "defaults",
                "checks",
                "starts",
                "active_request",
            }
        ):
            raise BridgeError("invalid project config fields")
        if data.get("runtime", "isolated") not in ("native", "isolated"):
            raise BridgeError("unsupported runtime")
        if "app_space" in data:
            app_space_id(data["app_space"])
        identifier(data["project_id"])
        validate_owner(data["owner"])
        if self.expected_binding is not None and self.expected_binding != (
            data["project_root"],
            data["owner"],
        ):
            raise BridgeError(
                "execution binding does not match project and caller",
                code="project_binding_mismatch",
            )
        if (
            not isinstance(data["project_root"], str)
            or not Path(data["project_root"]).is_absolute()
            or str(Path(data["project_root"]).resolve()) != data["project_root"]
            or not Path(data["project_root"]).is_dir()
        ):
            raise BridgeError("project root must be canonical and available")
        policy(data["defaults"], agent=selection_agent(data.get("agent", "pi")))
        checks_config(data["checks"])
        if not isinstance(data["starts"], dict):
            raise BridgeError("invalid project run bindings")
        if data["active_request"] is not None:
            identifier(data["active_request"])
            if data["active_request"] not in data["starts"]:
                raise BridgeError("invalid active request binding")
        pointers = set()
        for request_id, record in data["starts"].items():
            identifier(request_id)
            if not isinstance(record, dict) or set(record) - {
                "runtime",
                "app_space",
                "admission",
            } != {
                "run",
                "run_id",
                "request",
                "policy",
            }:
                raise BridgeError("invalid project start binding")
            if "admission" in record and record["admission"] not in ("shared", "abandoned"):
                raise BridgeError("invalid project start admission state")
            if record.get("admission") == "abandoned" and (
                data["active_request"] == request_id or record["run_id"] is not None
            ):
                raise BridgeError("abandoned start cannot bind an active run")
            if record.get("runtime", "isolated") not in ("native", "isolated"):
                raise BridgeError("unsupported bound runtime")
            if "app_space" in record:
                app_space_id(record["app_space"])
                if record.get("runtime", "isolated") != "native":
                    raise BridgeError("App space binding requires a native run")
            self.run_path(record["run"])
            if record["run"] in pointers:
                raise BridgeError("run pointer bound to multiple requests")
            pointers.add(record["run"])
            if record["run_id"] is not None:
                identifier(record["run_id"])
            requested = record["request"]
            if (
                not isinstance(requested, dict)
                or set(requested) - {"agent"} != {"development", "model", "thinking"}
                or requested["development"] is not None
                and not isinstance(requested["development"], dict)
            ):
                raise BridgeError("invalid saved project request")
            if not isinstance(record["policy"], dict) or set(record["policy"]) != {
                "requested",
                "resolved",
                "sources",
            }:
                raise BridgeError("invalid project execution policy")
            execution = record["policy"]
            agent = selection_agent(requested.get("agent", "pi"))
            resolved = policy(execution["resolved"], agent=agent)
            overrides = {name: requested[name] for name in ("model", "thinking")}
            default_sources = (
                ("project_default", "agent_default")
                if "agent" in requested
                else ("project_default",)
            )
            if execution["requested"] != overrides or all(
                execution["sources"]
                != {
                    name: "task_override" if value is not None else source
                    for name, value in overrides.items()
                }
                for source in default_sources
            ):
                raise BridgeError("inconsistent project execution policy")
            for name, value in overrides.items():
                if value is not None and value != resolved[name]:
                    raise BridgeError("project override differs from resolved execution policy")
        self.credential = None
        self.endpoint = None
        self.relay_home = None
        if data["webhook_config"] is not None:
            if (
                not isinstance(data["webhook_config"], str)
                or not Path(data["webhook_config"]).is_absolute()
            ):
                raise BridgeError("private webhook config reference must be absolute")
            notification = load_config(data["webhook_config"], data["owner"])
            self.credential, self.endpoint = notification.get("key"), notification.get("url")
            self.relay_home = notification.get("profile_home")
            if (
                self.relay_home is not None
                and self.expected_project_id is not None
                and data["project_id"] != self.expected_project_id
            ):
                raise BridgeError(
                    "Relay requires the registered project identity",
                    code="project_binding_mismatch",
                )
            _credential_free(data, self.credential)
            _credential_free(list(PUBLIC_FIELDS), self.credential)

    @contextmanager
    def locked(self):
        with self.file_lock(self.path):
            self.data = private_json(self.path)
            self.validate()
            yield

    def run_path(self, pointer):
        if not valid_run_pointer(pointer, legacy=True, project_entry=True):
            raise BridgeError("invalid project run pointer")
        root = Path(self.data["project_root"])
        path = root / pointer
        for ancestor in (path, path.parent, path.parent.parent):
            if ancestor.is_symlink():
                raise BridgeError("symlink in project run pointer")
        if path.resolve() != path or not path.is_relative_to(root):
            raise BridgeError("project run pointer escapes bound project")
        return path

    def public(self, value):
        private = [
            self.data["project_root"],
            str(self.path),
            self.data.get("webhook_config"),
            self.credential,
            self.endpoint,
            self.relay_home,
            *self.data["owner"].values(),
        ]
        private = sorted((item for item in private if item), key=len, reverse=True)

        def clean(item):
            if isinstance(item, dict):
                result = {}
                for key, val in item.items():
                    if key in ("owner", "webhook_config", "project_root"):
                        continue
                    name = key if key in PUBLIC_FIELDS else clean(key)
                    result[name] = clean(val) if name not in result else "<redacted key collision>"
                return result
            if isinstance(item, list):
                return [clean(val) for val in item]
            if isinstance(item, str):
                for secret in private:
                    if secret:
                        item = item.replace(secret, "<private>")
            return item

        cleaned = clean(value)
        _credential_free(cleaned, self.credential)
        return cleaned

    def configuration(self):
        with self.locked():
            return self.public(
                {
                    "project_id": self.data["project_id"],
                    "agent": self.data.get("agent", "pi"),
                    "defaults": dict(self.data["defaults"]),
                    "runtime": self.data.get("runtime", "isolated"),
                    "app_space": self.data.get("app_space"),
                    "checks": [check["name"] for check in self.data["checks"]],
                    "notification_configured": bool(self.data["webhook_config"]),
                    "active_request": self.data["active_request"],
                }
            )

    def runtime(self, value):
        if value not in ("native", "isolated"):
            raise BridgeError("unsupported runtime")
        with self.locked():
            self.data["runtime"] = value
            self.persist(self.path, self.data)
            return {
                "runtime": value,
                "applies_to": "future_starts",
                "running_session_changed": False,
            }

    def app_space(self, value):
        app_space_id(value)
        with self.locked():
            _credential_free(value, self.credential)
            self.data["app_space"] = value
            self.persist(self.path, self.data)
            return self.public(
                {
                    "app_space": value,
                    "applies_to": "future_starts",
                    "running_session_changed": False,
                }
            )

    def record(self, request_id=None):
        request_id = request_id or self.data["active_request"]
        if request_id is None:
            raise BridgeError("project has no bound run")
        identifier(request_id)
        if request_id not in self.data["starts"]:
            raise BridgeError("request is not bound to this project")
        return request_id, self.data["starts"][request_id]

    def manager(self, record):
        if record.get("admission") == "abandoned":
            raise BridgeError("start was abandoned before launch", code="project_start_abandoned")
        root = self.run_path(record["run"])
        if not (root / "manager.json").exists():
            raise BridgeError("creation unresolved; inspect existing run; never replay start")
        if (
            (root / "manager.json").is_symlink()
            or (root / "bridge").is_symlink()
            or (root / "bridge" / "journal.json").is_symlink()
        ):
            raise BridgeError("symlink in bound manager state")
        manager = Manager(root, self.data["owner"], runner=self.runner, sender=self.sender)
        if (
            manager.data.get("project") != self.data["project_root"]
            or manager.data.get("run") != record["run"]
            or record["run_id"] is not None
            and manager.data["run_id"] != record["run_id"]
        ):
            raise BridgeError("bound run identity mismatch")
        bridge = manager.bridge()
        if manager.data.get("agent", "codex") != record["request"].get("agent", "pi") or (
            bridge.agent() != record["request"].get("agent", "pi")
        ):
            raise BridgeError("bound coding agent changed; reconcile the original run")
        if bridge.data.get("runtime", "isolated") != record.get("runtime", "isolated"):
            raise BridgeError("bound runtime changed; reconcile the original run")
        reference = self.data["webhook_config"]
        expected = str(Path(reference).resolve()) if reference is not None else None
        if manager.data.get("webhook_config") != expected:
            raise BridgeError(
                "bound notification config changed; reconcile project/run binding before use"
            )
        if self.relay_home is not None:
            relay = manager.relay_context()
            if relay is None or relay["project_id"] != self.data["project_id"]:
                raise BridgeError("bound Relay project identity mismatch")
            if self.data["starts"].get(relay["request_id"]) != record:
                raise BridgeError("bound Relay request identity mismatch")
        if record["run_id"] is None:
            record["run_id"] = manager.data["run_id"]
            self.persist(self.path, self.data)
        return manager

    def envelope(self, request_id, record, result):
        return self.public(
            {
                "project_id": self.data["project_id"],
                "request_id": request_id,
                "execution_policy": record["policy"],
                **result,
            }
        )

    def app_state(self, record, *, bind=False):
        root, space = self.run_path(record["run"]), record["app_space"]
        path = root / "app.json"
        if bind:
            try:
                from herdr_agent_bridge.app_session import AppSession

                if path.exists() or path.is_symlink():
                    raise BridgeError("App association already exists; inspect original run")
                app = AppSession(root)
                app.bridge.runner = self.runner
                app.start()
                app.bind(space)
            except (
                BridgeError,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                RecursionError,
                subprocess.TimeoutExpired,
                KeyboardInterrupt,
            ):
                # Agent creation has already succeeded. Preserve its identity even
                # when attachment or label delivery is uncertain; never retry here.
                pass
        binding = "pending"
        visible = None
        association_closed = False
        try:
            saved = private_json(path)
            if (
                isinstance(saved, dict)
                and saved.get("runtime") == "native"
                and saved.get("launch") == "attached"
                and saved.get("binding") in ("not_attempted", "unknown", "bound")
                and (saved.get("binding") == "not_attempted" or saved.get("space") == space)
            ):
                binding = saved["binding"]
                association_closed = saved.get("closed") is True
                if binding == "bound":
                    binding = "unknown"  # A bound claim requires verified saved coordinates.
                    visible = saved.get("visible")
                    journal = private_json(root / "bridge" / "journal.json")
                    address = journal.get("address") if isinstance(journal, dict) else None
                    valid_location = (
                        isinstance(address, str)
                        and bool(address)
                        and saved.get("address") == address
                        and isinstance(visible, list)
                        and len(visible) == 1
                        and isinstance(visible[0], dict)
                        and set(visible[0]) == {"space", "tab", "pane", "terminal"}
                        and visible[0]["space"] == space
                        and visible[0]["terminal"] == address
                        and all(
                            isinstance(visible[0][key], str) and visible[0][key]
                            for key in ("tab", "pane")
                        )
                    )
                    if valid_location:
                        binding = "bound"
        except BridgeError:
            pass
        result = {
            "space": space,
            "binding": binding,
            "pending": binding != "bound",
            "evidence": "saved_app_state",
        }
        if association_closed:
            result["closed"] = True
        if binding == "bound":
            result["visible"] = visible
        else:
            result["error"] = (
                "App association not confirmed; inspect original run without replaying start"
            )
        return result

    def start(
        self,
        request_id,
        development=None,
        *,
        agent=None,
        model=None,
        thinking=None,
        source_home=None,
        require_shared=False,
    ):
        import secrets

        identifier(request_id)
        # Requested values are preserved independently from subsequently changed defaults.
        requested = {"development": development, "model": model, "thinking": thinking}
        requested = json.loads(json.dumps(requested, allow_nan=False))
        # Lock order is canonical project, then this entry's config.
        project_root = self.data["project_root"]
        with self._admission_lock(project_root) as slot_path, self.locked():
            if self.data["project_root"] != project_root:
                raise BridgeError("project root changed; reopen the project entry")
            if request_id in self.data["starts"]:
                record = self.data["starts"][request_id]
                original = dict(record["request"])
                original_agent = original.pop("agent", "pi")
                if original != requested or agent is not None and agent != original_agent:
                    raise BridgeError(
                        "request identifier already has different start parameters",
                        code="project_start_mismatch",
                    )
                result = self.manager(record).status()
                if "app_space" in record:
                    result["app"] = self.app_state(record)
                return self.envelope(request_id, record, result)
            default_agent = self.data.get("agent", "pi")
            agent = selection_agent(default_agent if agent is None else agent)
            execution = ExecutionPolicy.resolve(
                self.data["defaults"] if agent == default_agent else AGENT_DEFAULTS[agent],
                agent=agent,
                model=model,
                thinking=thinking,
                default_source="project_default" if agent == default_agent else "agent_default",
            )
            resolved = execution.resolved
            runtime = self.data.get("runtime", "isolated")
            if development is not None:
                require_development_runtime(agent, runtime)
            if runtime == "native" and source_home is not None:
                raise BridgeError(
                    "native runtime uses the normal agent home", code="execution_policy_invalid"
                )
            validate_agent_selection(
                agent, resolved["model"], resolved["thinking"], runtime, project=project_root
            )
            state = self._admission_state(slot_path, project_root)
            if require_shared and state is None:
                raise BridgeError(
                    "shared starts must be enabled by the project operator",
                    code="shared_start_required",
                )
            self._check_shared_start(state)
            # Keep the per-binding guard for legacy starts and as a fallback
            # if the shared slot does not account for a local active request.
            if self.data["active_request"] is not None:
                _, previous = self.record()
                if not self.manager(previous).status()["closed"]:
                    raise BridgeError(
                        "one bound active run per project; close the original run first"
                    )
            record = {
                "run": str(RUNS / (self.data["project_id"] + "-" + secrets.token_hex(12))),
                "run_id": None,
                "request": {**requested, "agent": agent},
                "runtime": runtime,
                "policy": execution.to_dict(),
            }
            if record["runtime"] == "native" and "app_space" in self.data:
                record["app_space"] = self.data["app_space"]
            if state is not None:
                record["admission"] = "shared"
            contract = self.contract(development) if development is not None else None
            run = self.run_path(record["run"])
            _credential_free([request_id, record], self.credential)
            self.data["starts"][request_id] = record
            self.data["active_request"] = request_id
            # Both the origin intent and the shared pointer are durable before
            # Manager can create resources. A crash between writes cannot launch.
            self.persist(self.path, self.data)
            if state is not None:
                state["active"] = self._slot(request_id, record)
                self.persist(slot_path, state)
            manager = Manager.create(
                run,
                self.data["project_root"],
                self.data["owner"],
                model=resolved["model"],
                thinking=resolved["thinking"],
                webhook_config=self.data["webhook_config"],
                source_home=source_home,
                runner=self.runner,
                sender=self.sender,
                agent=agent,
                development=contract,
                runtime=record["runtime"],
                relay_context={"project_id": self.data["project_id"], "request_id": request_id}
                if self.relay_home is not None
                else None,
            )
            record["run_id"] = manager.data["run_id"]
            self.persist(self.path, self.data)
            result = manager.status()
            if "app_space" in record:
                result["app"] = self.app_state(record, bind=True)
            return self.envelope(request_id, record, result)

    def contract(self, spec):
        if not isinstance(spec, dict):
            raise BridgeError("development contract must be an object")
        spec = json.loads(json.dumps(spec))
        names = spec.get("checks")
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(name, str) for name in names)
        ):
            raise BridgeError("development checks must name configured project checks")
        configured = {check["name"]: check for check in self.data["checks"]}
        if len(set(names)) != len(names) or any(name not in configured for name in names):
            raise BridgeError("unknown or duplicate configured check")
        spec["checks"] = [configured[name] for name in names]
        # Validate paths/contract before reserving start; Manager still owns baseline/gate.
        from herdr_agent_bridge.development import prepare

        prepare(self.data["project_root"], spec)
        return spec

    def status(self, request_id=None, *, observe=False, wait=0, task_id=None):
        with self.locked():
            request_id, record = self.record(request_id)
            manager = self.manager(record)
            result = (
                manager.observe(task_id, wait)
                if observe and not manager.data["closed"]
                else manager.status(task_id)
            )
            if "app_space" in record:
                result["app"] = self.app_state(record)
            return self.envelope(request_id, record, result)

    def close(self, request_id=None):
        with self.locked():
            request_id, record = self.record(request_id)
            return self.envelope(request_id, record, self.manager(record).close())

    def defaults(self, *, agent=None, model=None, thinking=None):
        if agent is None and model is None and thinking is None:
            return self.configuration()
        with self.locked():
            previous = self.data.get("agent", "pi")
            agent = selection_agent(previous if agent is None else agent)
            updated = ExecutionPolicy.resolve(
                self.data["defaults"] if agent == previous else AGENT_DEFAULTS[agent],
                agent=agent,
                model=model,
                thinking=thinking,
            ).resolved
            _credential_free([agent, updated], self.credential)
            self.data["agent"] = agent
            self.data["defaults"] = updated
            self.persist(self.path, self.data)
            return self.public(
                {
                    "project_id": self.data["project_id"],
                    "agent": agent,
                    "defaults": updated,
                    "applies_to": "future_starts",
                    "running_session_changed": False,
                }
            )

    def submit(self, task_id, prompt, *, parent=None, request_id=None, monitor=None):
        identifier(task_id)
        with self.locked():
            _credential_free([task_id, prompt, parent], self.credential)
            request_id, record = self.record(request_id)
            manager = self.manager(record)
            bridge = manager.bridge()
            development_task = (
                (bridge.data.get("development") or {}).get("contract", {}).get("task_id")
            )
            result = manager.submit(task_id, prompt, parent, development_task=development_task)
            if monitor is None:
                monitor = bridge.data.get("runtime") == "native" and bool(
                    manager.data.get("webhook_config")
                )
            if monitor:
                try:
                    result.update(manager.monitor())
                    result["next_action"] = "await_callback"
                except (BridgeError, OSError, subprocess.TimeoutExpired):
                    result["monitor_error"] = "observer launch unresolved; inspect existing run"
                    result["next_action"] = "inspect_monitor_without_resubmitting"
            return self.envelope(request_id, record, result)

    def result(self, request_id=None, task_id=None):
        return self.status(request_id, task_id=task_id)

    def interrupt(self, task_id, *, actor, note, request_id=None):
        with self.locked():
            _credential_free([task_id, actor, note], self.credential)
            request_id, record = self.record(request_id)
            result = self.manager(record).interrupt(task_id, actor=actor, note=note)
            return self.envelope(request_id, record, result)

    def native_command(self, operation_id, command, arguments=None, *, request_id):
        with self.locked():
            _credential_free([operation_id, command, arguments], self.credential)
            request_id, record = self.record(identifier(request_id))
            manager = self.manager(record)
            development_task = (
                (manager.bridge().data.get("development") or {}).get("contract", {}).get("task_id")
            )
            result = manager.native_command(
                operation_id, command, arguments, development_task=development_task
            )
            return self.envelope(request_id, record, result)

    def verify(self, request_id=None, *, task_id=None, contract=None):
        with self.locked():
            request_id, record = self.record(request_id)
            manager = self.manager(record)
            if manager.bridge().data.get("development"):
                if contract is not None:
                    raise BridgeError(
                        "development verification uses the bound development contract"
                    )
                if task_id is not None:
                    identifier(task_id)
                development = True
            else:
                if task_id is None or contract is None:
                    raise BridgeError(
                        "read-only verification requires round identifier and named contract"
                    )
                development = False
        result = (
            manager.verify_development(expected_round=task_id)
            if development
            else manager.verify(task_id, contract)
        )
        return self.envelope(request_id, record, result)

    def decide(self, decision, actor, note, request_id=None):
        with self.locked():
            _credential_free([actor, note], self.credential)
            request_id, record = self.record(request_id)
            return self.envelope(
                request_id, record, self.manager(record).decide_development(decision, actor, note)
            )

    def control(self, action, request_id=None, **kwargs):
        with self.locked():
            _credential_free(kwargs, self.credential)
            request_id, record = self.record(request_id)
            manager = self.manager(record)
            result = (
                manager.recovery_control(action, **kwargs)
                if action in ("recovery-status", "recover")
                else manager.development_control(action, **kwargs)
            )
            return self.envelope(request_id, record, result)

    @staticmethod
    def notification_summary(event):
        return {
            "event_id": event["payload"]["event_id"],
            "delivery": event["state"],
            "acknowledged": event.get("acknowledgment") is not None,
            "receipt": (event.get("acknowledgment") or {}).get("receipt"),
            **({"relay": event["relay"]} if "relay" in event else {}),
        }

    def notify(self, event_id, request_id=None, *, retry=False):
        with self.locked():
            request_id, record = self.record(request_id)
            manager = self.manager(record)
            event = manager.notify(event_id, retry)
            return self.envelope(
                request_id, record, {"notification": self.notification_summary(event)}
            )

    def monitor(self, request_id=None, *, seconds=600, launcher=None):
        with self.locked():
            request_id, record = self.record(request_id)
            manager = self.manager(record)
            result = (
                manager.monitor(seconds)
                if launcher is None
                else manager.monitor(seconds, launcher=launcher)
            )
            return self.envelope(request_id, record, result)

    def notification(self, payload, *, receipt=None):
        # A notification is an untrusted pointer, never an instruction or a new task.
        from herdr_agent_bridge.grok_notifications import _payload

        with self.locked():
            payload = _payload(payload, self.data["owner"])
            if "run" not in payload:
                raise BridgeError("notification requires a bound relative run pointer")
            self.run_path(payload["run"])
            matches = [
                (request_id, record)
                for request_id, record in self.data["starts"].items()
                if record["run"] == payload["run"]
            ]
            if len(matches) != 1:
                raise BridgeError("notification run is not bound to this project")
            request_id, record = matches[0]
            manager = self.manager(record)
            if (
                payload["run_id"] != record["run_id"]
                or payload["event_id"] not in manager.data["events"]
            ):
                raise BridgeError("notification run/event identity mismatch")
            event = manager.outbox().get(payload["event_id"])
            if event["payload"] != payload:
                raise BridgeError("notification differs from saved event identity")
            # Query current state before reading the result or acknowledging. Closed
            # runs stay readable, but cannot be observed/submitted as live work.
            latest = manager.status() if manager.data["closed"] else manager.observe()
            if receipt is not None:
                event = manager.acknowledge(payload["event_id"], receipt)
            return self.envelope(
                request_id, record, {**latest, "notification": self.notification_summary(event)}
            )

    def acknowledge(self, event_id, receipt, request_id=None):
        # Use the same validation and refresh path even without an incoming payload.
        with self.locked():
            _, record = self.record(request_id)
            manager = self.manager(record)
            if event_id not in manager.data["events"]:
                raise BridgeError("event does not belong to bound run")
            payload = manager.outbox().get(event_id)["payload"]
        return self.notification(payload, receipt=receipt)


def add_native_command_arguments(parser):
    parser.add_argument("operation_id")
    parser.add_argument("native_command", metavar="COMMAND")
    parser.add_argument("--arguments")


def main(argv=None, *, bound_config=None):
    import argparse
    import sys

    from herdr_agent_bridge.readonly_contracts import PROMPTS

    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Installed CLI: --version; personal --help for multi-project use; "
            "agent / manager / app / tool NAME for lower-level and acceptance commands."
        ),
    )
    if bound_config is None:
        parser.add_argument(
            "--config", type=Path, required=True, help="private 0600 project config"
        )
    else:
        parser.set_defaults(config=Path(bound_config))
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--project-id", required=True)
    initialize.add_argument("--project", required=True, type=Path)
    initialize.add_argument("--bot-id", required=True)
    initialize.add_argument("--conversation-id", required=True)
    initialize.add_argument("--webhook-config", "--notification-config", type=Path)
    initialize.add_argument("--checks-file", type=Path)
    initialize.add_argument("--agent", choices=AGENTS, default="pi")
    initialize.add_argument("--runtime", choices=("native", "isolated"), default="native")
    initialize.add_argument(
        "--app-space", help="existing local App space id for future native starts"
    )
    commands.add_parser("config")
    runtime = commands.add_parser(
        "runtime", help="choose runtime for future starts; existing runs retain their identity"
    )
    runtime.add_argument("value", choices=("native", "isolated"))
    space = commands.add_parser(
        "app-space", help="choose the existing App space for future native starts"
    )
    space.add_argument("value", metavar="SPACE_ID")
    defaults = commands.add_parser("defaults", help="change defaults for future starts only")
    defaults.add_argument("--agent", choices=AGENTS)
    defaults.add_argument("--model")
    defaults.add_argument("--thinking", "--reasoning")
    start = commands.add_parser("start")
    start.add_argument("request_id")
    start.add_argument("--development-contract", type=Path)
    start.add_argument("--agent", choices=AGENTS)
    start.add_argument("--model")
    start.add_argument("--thinking", "--reasoning")
    if bound_config is None:
        abandon = commands.add_parser(
            "abandon-start", help="operator: release a provably unlaunched start"
        )
        abandon.add_argument("request_id")
    for name in (
        "status",
        "result",
        "submit",
        "verify",
        "decide",
        "close",
        "ack",
        "notify",
        "monitor",
        "takeover",
        "resume",
        "reconcile",
        "interrupt",
        "native-command",
        "recovery-status",
        "recover",
    ):
        command = commands.add_parser(name)
        command.add_argument("--request-id", required=name == "native-command")
        if name in ("status", "result", "verify"):
            command.add_argument("--task-id")
        if name == "status":
            command.add_argument("--observe", action="store_true")
            command.add_argument("--wait", type=int, default=0)
        elif name == "submit":
            command.add_argument("task_id")
            command.add_argument("--prompt-file", type=Path, required=True)
            command.add_argument("--parent")
            command.add_argument(
                "--monitor",
                action="store_true",
                default=None,
                help="start callback observation (automatic for native runs with notifications)",
            )
        elif name == "verify":
            command.add_argument("--contract", choices=tuple(PROMPTS))
        elif name == "decide":
            command.add_argument("decision", choices=("accept", "reject", "terminate"))
            command.add_argument("--actor", required=True)
            command.add_argument("--note", required=True)
        elif name in ("ack", "notify"):
            command.add_argument("event_id")
            if name == "ack":
                command.add_argument("--receipt", required=True)
            else:
                command.add_argument("--retry", action="store_true")
        elif name == "monitor":
            command.add_argument("--seconds", type=int, default=600)
        elif name == "takeover":
            command.add_argument("--actor", required=True)
        elif name == "interrupt":
            command.add_argument("task_id")
            command.add_argument("--actor", required=True)
            command.add_argument("--note", required=True)
        elif name == "native-command":
            add_native_command_arguments(command)
        elif name == "resume":
            command.add_argument("--fingerprint", required=True)
            command.add_argument("--review", required=True)
        elif name == "recover":
            command.add_argument("--fingerprint", required=True)
            command.add_argument("--actor", required=True)
            command.add_argument("--review", required=True)
    notification = commands.add_parser("notification")
    notification.add_argument("--payload-file", type=Path, required=True)
    notification.add_argument("--receipt")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            entry = Project.initialize(
                args.config,
                args.project_id,
                args.project,
                {"bot_id": args.bot_id, "conversation_id": args.conversation_id},
                agent=args.agent,
                webhook_config=args.webhook_config,
                runtime=args.runtime,
                app_space=args.app_space,
                checks=input_json(args.checks_file) if args.checks_file else [],
            )
            result = entry.configuration()
        else:
            entry = Project(args.config)
            if args.command == "config":
                result = entry.configuration()
            elif args.command == "runtime":
                result = entry.runtime(args.value)
            elif args.command == "app-space":
                result = entry.app_space(args.value)
            elif args.command == "defaults":
                result = entry.defaults(agent=args.agent, model=args.model, thinking=args.thinking)
            elif args.command == "start":
                result = entry.start(
                    args.request_id,
                    input_json(args.development_contract) if args.development_contract else None,
                    agent=args.agent,
                    model=args.model,
                    thinking=args.thinking,
                )
            elif args.command == "abandon-start":
                result = entry.abandon_start(args.request_id)
            elif args.command == "status":
                result = entry.status(
                    args.request_id, observe=args.observe, wait=args.wait, task_id=args.task_id
                )
            elif args.command == "result":
                result = entry.result(args.request_id, args.task_id)
            elif args.command == "submit":
                result = entry.submit(
                    args.task_id,
                    args.prompt_file.read_text(),
                    parent=args.parent,
                    request_id=args.request_id,
                    monitor=args.monitor,
                )
            elif args.command == "verify":
                result = entry.verify(args.request_id, task_id=args.task_id, contract=args.contract)
            elif args.command == "decide":
                result = entry.decide(args.decision, args.actor, args.note, args.request_id)
            elif args.command == "close":
                result = entry.close(args.request_id)
            elif args.command == "notification":
                result = entry.notification(input_json(args.payload_file), receipt=args.receipt)
            elif args.command == "ack":
                result = entry.acknowledge(args.event_id, args.receipt, args.request_id)
            elif args.command == "notify":
                result = entry.notify(args.event_id, args.request_id, retry=args.retry)
            elif args.command == "monitor":
                result = entry.monitor(args.request_id, seconds=args.seconds)
            elif args.command == "interrupt":
                result = entry.interrupt(
                    args.task_id, actor=args.actor, note=args.note, request_id=args.request_id
                )
            elif args.command == "native-command":
                result = entry.native_command(
                    args.operation_id,
                    args.native_command,
                    args.arguments,
                    request_id=args.request_id,
                )
            else:
                result = entry.control(
                    args.command,
                    args.request_id,
                    **{
                        key: getattr(args, key)
                        for key in ("actor", "fingerprint", "review")
                        if hasattr(args, key)
                    },
                )
        print(json.dumps(entry.public(result), ensure_ascii=False, indent=2))
        return 1 if result.get("passed") is False else 0
    except (
        BridgeError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        subprocess.TimeoutExpired,
        KeyboardInterrupt,
    ) as exc:
        # Native errors, parsed input and paths may contain private material. Do
        # not print their text, source JSON, commands, tracebacks or credentials.
        detail = str(exc) if isinstance(exc, BridgeError) and not isinstance(exc, HcError) else None
        code = exc.code if isinstance(exc, BridgeError) else "project_operation_failed"
        refused = (
            args.command == "start"
            and code in START_REFUSALS
            or (args.command == "native-command" and code in NATIVE_COMMAND_REFUSALS)
            or (args.command == "submit" and code in SUBMIT_REFUSALS)
        )
        failure = {
            "error": "project_operation_failed",
            "detail": detail,
            "code": code,
            "operation": args.command,
            "delivery": "not_attempted" if refused else "unknown",
        }
        if not refused:
            failure["action"] = "inspect the bound project/run before retrying"
        elif code in SHARED_START_REFUSALS:
            failure["action"] = "ask the project operator to resolve the shared start gate"
        if "entry" in locals():
            try:
                failure = entry.public(failure)
            except BridgeError:
                failure = {}  # No unsafe fallback text if even redaction cannot be emitted.
        print(json.dumps(failure), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
