"""Pi adapter regressions with isolated source homes and a disposable fake hc.

These tests do not execute Pi, Codex, hc, or a network service. Native Pi v3
history below is a fixture, not evidence of a real coding-agent integration.
"""

import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_pi_evidence import two_pack_history

from herdr_agent_bridge import agent_bridge, grok_bridge, readiness, readonly_contracts
from herdr_agent_bridge.resource_paths import resource_path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

OWNER = {"bot_id": "fixture-bot", "conversation_id": "fixture-conversation"}
STAMP = "2026-01-02T03:04:05.000Z"


class FakeHC:
    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.terminal = {
            "address": "fixture-terminal",
            "state": "live",
            "turn": 0,
            "status": {"state": "idle"},
            "created_ms": 0,
        }

    def __call__(self, args, env, timeout):
        self.calls.append((list(args), dict(env), timeout))
        command = args[0]
        if command in self.handlers:
            return self.handlers[command](args, env, timeout)
        if command == "run":
            return {"address": self.terminal["address"]}
        if command == "get":
            if self.terminal is None:
                raise agent_bridge.HcError({"stderr": json.dumps({"error": {"kind": "not_found"}})})
            return copy.deepcopy(self.terminal)
        if command == "send":
            return {"started": True}
        if command == "wait":
            return {"reason": "timeout"}
        if command == "read":
            return {"text": "PRIVATE-FIXTURE-SCREEN"}
        if command == "kill":
            self.terminal["state"] = "finished"
        if command == "rm":
            self.terminal = None
        if command == "ls":
            return {"terminals": [copy.deepcopy(self.terminal)] if self.terminal else []}
        return {}

    def count(self, command):
        return sum(args[0] == command for args, _, _ in self.calls)


def session_header(project):
    return {
        "type": "session",
        "version": 3,
        "id": "pi-session-01",
        "cwd": str(project),
        "timestamp": STAMP,
    }


def user_message(entry_id, parent_id, text):
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": STAMP,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "timestamp": 1767323045000,
        },
    }


def assistant_message(
    entry_id, parent_id, task_id, answer, kind="result", provider="provider-a", model="model-a"
):
    result = {"task_id": task_id, "kind": kind, "answer": answer}
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": STAMP,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": json.dumps(result)}],
            "api": "fixture-api",
            "provider": provider,
            "model": model,
            "usage": {},
            "stopReason": "stop",
            "timestamp": 1767323046000,
        },
    }


class PiBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-pi-bridge-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.source = self.base / "source-pi"
        self.source.mkdir()
        self.settings = {
            "defaultProvider": "provider-a",
            "defaultModel": "model-a",
            "defaultThinkingLevel": "medium",
            "lastChangelogVersion": "fixture-version",
            "packages": ["fixture-package"],
            "extensions": ["fixture-extension"],
            "mcpServers": {"fixture": {"command": "untrusted"}},
            "trustedProjects": [str(self.project)],
            "theme": "fixture-theme",
            "compaction": {"enabled": True},
            "retry": {"enabled": True},
        }
        self.models = {
            "providers": {
                "provider-a": {
                    "baseUrl": "https://provider-a.invalid",
                    "api": "fixture-api",
                    "apiKey": "PRIVATE-FIXTURE-A-MODEL-KEY",
                    "models": [{"id": "model-a", "name": "Model A"}],
                },
                "provider-b": {
                    "baseUrl": "https://provider-b.invalid",
                    "api": "fixture-api",
                    "apiKey": "PRIVATE-FIXTURE-B-MODEL-KEY",
                    "models": [{"id": "model-b", "name": "Model B"}],
                },
            }
        }
        self.auth = {
            "provider-a": {"type": "api_key", "key": "PRIVATE-FIXTURE-A-AUTH"},
            "provider-b": {"type": "api_key", "key": "PRIVATE-FIXTURE-B-AUTH"},
        }
        self.write_json(self.source / "settings.json", self.settings)
        self.write_json(self.source / "models.json", self.models)
        self.write_json(self.source / "auth.json", self.auth)
        for name in (
            "AGENTS.md",
            "extensions/extension.ts",
            "packages/pkg.json",
            "prompts/prompt.md",
            "themes/theme.json",
            "sessions/old.jsonl",
        ):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE-FIXTURE-UNRELATED\n")
        self.hc = FakeHC()

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value))

    def tree(self, root):
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def make_bridge(self, model=None):
        obj = agent_bridge.Bridge.create(
            self.base / "run",
            self.project,
            model=model,
            source_home=self.source,
            runner=self.hc,
            agent="pi",
        )
        self.addCleanup(shutil.rmtree, Path(obj.data["scratch"]), ignore_errors=True)
        return obj

    def make_manager(self):
        root = self.project / ".artifacts" / "runs" / "fixture-run"
        manager = grok_bridge.Manager.create(
            root, self.project, OWNER, source_home=self.source, runner=self.hc, agent="pi"
        )
        self.addCleanup(shutil.rmtree, Path(manager.bridge().data["scratch"]), ignore_errors=True)
        return manager

    def write_history(self, obj, entries):
        path = Path(obj.data["scratch"]) / "pi" / "sessions" / "fixture.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        return path

    def selected_task_fixture(self):
        manager = grok_bridge.Manager.create(
            self.project / ".artifacts" / "runs" / "selected-fixture",
            self.project,
            OWNER,
            source_home=self.source,
            runner=self.hc,
            agent="pi",
            model="provider-a/model-a",
            thinking="medium",
        )
        obj = manager.bridge()
        self.addCleanup(shutil.rmtree, Path(obj.data["scratch"]), ignore_errors=True)
        self.write_json(
            obj.root / "pi-selection.json",
            {
                "state": "ready",
                "model": "provider-a/model-a",
                "thinking": "medium",
                "session_id": "pi-session-01",
            },
        )
        manager.submit("first", "Inspect only")
        submitted = manager.bridge().task("first")["submitted_text"]
        entries = [
            session_header(self.project),
            {
                "type": "model_change",
                "id": "m",
                "parentId": None,
                "timestamp": STAMP,
                "provider": "provider-a",
                "modelId": "model-a",
            },
            {
                "type": "thinking_level_change",
                "id": "t",
                "parentId": "m",
                "timestamp": STAMP,
                "thinkingLevel": "medium",
            },
            user_message("u", "t", submitted),
            assistant_message("a", "u", "first", {"value": "old"}),
        ]
        self.write_history(obj, entries)
        self.hc.terminal.update(turn=1, status={"state": "done"})
        return manager, obj, entries

    def test_verification_is_cleared_when_final_changes(self):
        manager, obj, entries = self.selected_task_fixture()
        self.assertEqual(manager.observe("first")["task"]["observed"], "evidence_ready")
        manager.bridge().verify("first", [{"check": "old answer checked", "passed": True}])
        self.assertTrue(manager.observe("first")["task"]["verified_success"])

        entries.append(assistant_message("a2", "a", "first", {"value": "new"}))
        self.write_history(obj, entries)
        result = manager.observe("first")["task"]
        self.assertEqual(result["evidence"]["result"]["answer"], {"value": "new"})
        self.assertFalse(result["verified_success"])

    def test_pending_followup_preserves_completed_previous_evidence_and_blocks_input(self):
        manager, obj, entries = self.selected_task_fixture()
        manager.observe("first")
        manager.bridge().verify("first", [{"check": "old answer checked", "passed": True}])
        manager.submit("second", "Follow up", parent="first")
        self.hc.terminal["status"]["state"] = "working"
        pending = manager.observe("second")
        earlier = manager.status("first")["task"]
        self.assertEqual(earlier["observed"], "evidence_ready")
        self.assertEqual(earlier["evidence"]["result"]["answer"], {"value": "old"})
        self.assertTrue(earlier["verified_success"])
        self.assertNotIn("diagnostic", earlier)
        self.assertEqual(pending["task"]["diagnostic"]["reason"], "native_no_match")
        self.assertIsNone(pending["task"]["evidence"]["result"])
        self.assertFalse(pending["input_ready"])
        with self.assertRaises(agent_bridge.BridgeError):
            manager.submit("third", "Do not send", parent="second")
        self.assertEqual(self.hc.count("send"), 2)

        submitted = manager.bridge().task("second")["submitted_text"]
        entries.append(user_message("u2", "a", submitted))
        path = self.write_history(obj, entries)
        incomplete = manager.observe("second")
        self.assertEqual(incomplete["task"]["diagnostic"]["reason"], "native_turn_incomplete")
        self.assertTrue(manager.status("first")["task"]["verified_success"])
        with path.open("ab") as stream:
            stream.write(b'{"type":')
        trailing = manager.observe("second")
        self.assertEqual(trailing["task"]["diagnostic"]["reason"], "native_trailing_write")
        self.assertIsNone(trailing["task"]["evidence"]["result"])
        self.assertTrue(manager.status("first")["task"]["verified_success"])
        self.assertFalse(trailing["input_ready"])

    def test_native_completed_followup_preserves_prior_settled_turn(self):
        manager, obj, entries = self.selected_task_fixture()
        native = manager.bridge()
        native.data["runtime"] = "native"
        native.data["native_env"] = {}
        native.persist()
        self.hc.terminal["labels"] = {"bridge_run": native.data["session"]}
        self.write_json(
            obj.root / "pi-settled.json",
            {
                "state": "settled",
                "aborted": False,
                "session_id": "pi-session-01",
                "final_message_id": "a",
            },
        )
        self.assertEqual(manager.observe("first")["task"]["observed"], "evidence_ready")
        manager.bridge().verify("first", [{"check": "first answer checked", "passed": True}])
        manager.submit("second", "Follow up", parent="first")
        submitted = manager.bridge().task("second")["submitted_text"]
        entries.extend(
            [
                user_message("u2", "a", submitted),
                assistant_message("a2", "u2", "second", {"value": "new"}),
            ]
        )
        self.write_history(obj, entries)
        self.write_json(
            obj.root / "pi-settled.json",
            {
                "state": "settled",
                "aborted": False,
                "session_id": "pi-session-01",
                "final_message_id": "a2",
            },
        )
        self.hc.terminal.update(turn=2, status={"state": "done"})
        self.assertEqual(manager.observe("second")["task"]["observed"], "evidence_ready")
        earlier = manager.status("first")["task"]
        self.assertEqual(earlier["observed"], "evidence_ready")
        self.assertEqual(earlier["evidence"]["result"]["answer"], {"value": "old"})
        self.assertTrue(earlier["verified_success"])

    def test_legacy_native_failure_is_revalidated_after_repair(self):
        manager, obj, entries = self.selected_task_fixture()
        old = manager.bridge()
        old.data["selection_error"] = "native_dispatch_mismatch"
        old.persist()
        invalid = copy.deepcopy(entries)
        invalid[-1]["message"]["model"] = "other-model"
        self.write_history(obj, invalid)
        failed = manager.observe("first")
        self.assertEqual(failed["model_selection"]["reason"], "native_dispatch_mismatch")
        self.assertFalse(failed["input_ready"])
        self.assertIsNone(failed["task"]["evidence"]["result"])

        self.write_history(obj, entries)
        recovered = manager.observe("first")
        self.assertEqual(recovered["model_selection"]["native_evidence"], "matched")
        self.assertEqual(recovered["task"]["observed"], "evidence_ready")
        self.assertEqual(recovered["task"]["evidence"]["result"]["answer"], {"value": "old"})
        self.assertEqual(self.hc.count("send"), 1)

    def test_legacy_unbound_verification_is_not_promoted(self):
        manager, _, _ = self.selected_task_fixture()
        manager.observe("first")
        manager.bridge().verify("first", [{"check": "old verification", "passed": True}])
        legacy = manager.bridge()
        legacy.task("first").pop("verification_basis")
        legacy.persist()
        self.assertFalse(manager.status("first")["task"]["verified_success"])
        self.assertFalse(manager.observe("first")["task"]["verified_success"])
        manager.bridge().verify("first", [{"check": "new verification", "passed": True}])
        self.assertTrue(manager.status("first")["task"]["verified_success"])

        legacy = manager.bridge()
        legacy.task("first").pop("verification_basis")
        legacy.persist()
        manager.close()
        self.assertFalse(manager.status("first")["task"]["verified_success"])

    def test_status_withholds_verified_result_after_native_history_is_invalidated(self):
        manager, obj, entries = self.selected_task_fixture()
        manager.observe("first")
        manager.bridge().verify("first", [{"check": "old verification", "passed": True}])
        self.write_history(obj, [{**entries[0], "version": 4}])
        status = manager.status("first")
        self.assertEqual(status["model_selection"]["reason"], "native_version_unsupported")
        self.assertFalse(status["task"]["verified_success"])
        self.assertIsNone(status["task"]["evidence"]["result"])

        failed = manager.observe("first")
        self.assertFalse(failed["task"]["verified_success"])
        self.write_history(obj, entries)
        recovered = manager.observe("first")
        self.assertEqual(recovered["task"]["observed"], "evidence_ready")
        self.assertFalse(recovered["task"]["verified_success"])

    def test_observe_reports_latest_pending_reason_if_history_changes_between_reads(self):
        manager, obj, _ = self.selected_task_fixture()
        path = Path(obj.data["scratch"]) / "pi" / "sessions" / "fixture.jsonl"
        original = Path.read_bytes
        reads = 0
        final = json.dumps(assistant_message("a2", "a", "first", {"value": "new"})).encode()

        def append_before_second_read(candidate):
            nonlocal reads
            if candidate == path:
                reads += 1
                if reads == 2:
                    with candidate.open("ab") as stream:
                        stream.write(final[:12])
                    snapshot = original(candidate)
                    with candidate.open("ab") as stream:
                        stream.write(final[12:] + b"\n")
                    return snapshot
            return original(candidate)

        with patch.object(Path, "read_bytes", append_before_second_read):
            observed = manager.observe("first")
        self.assertEqual(observed["model_selection"]["reason"], "native_trailing_write")
        self.assertEqual(observed["task"]["diagnostic"]["reason"], "native_trailing_write")
        self.assertIsNone(observed["task"]["evidence"]["result"])
        self.assertFalse(observed["input_ready"])
        recovered = manager.observe("first")
        self.assertEqual(recovered["model_selection"]["native_evidence"], "matched")
        self.assertEqual(recovered["task"]["evidence"]["result"]["answer"], {"value": "new"})

    def test_recovered_parser_failure_gets_new_id_but_same_episode_is_idempotent(self):
        manager, obj, entries = self.selected_task_fixture()
        bad = {**session_header(self.project), "version": 4}
        self.write_history(obj, [bad])
        first = manager.observe("first")
        legacy = json.loads(manager.path.read_text())
        legacy.pop("agent_error_episodes")
        self.write_json(manager.path, legacy)
        manager = grok_bridge.Manager(manager.root, OWNER, runner=self.hc)
        repeated = manager.observe("first")
        self.assertEqual(first["event_id"], repeated["event_id"])
        self.assertEqual(manager.outbox().get(first["event_id"])["payload"]["kind"], "agent_error")

        self.write_history(obj, [])
        waiting = manager.observe("first")
        self.assertEqual(waiting["model_selection"]["reason"], "native_session_incomplete")
        self.write_history(obj, [bad])
        self.assertEqual(manager.observe("first")["event_id"], first["event_id"])

        self.write_history(obj, entries)
        self.assertEqual(manager.observe("first")["task"]["observed"], "evidence_ready")
        self.write_history(obj, [bad])
        manager = grok_bridge.Manager(manager.root, OWNER, runner=self.hc)
        second = manager.observe("first")
        self.assertNotEqual(second["event_id"], first["event_id"])
        self.assertEqual(manager.observe("first")["event_id"], second["event_id"])
        self.assertEqual(manager.outbox().get(second["event_id"])["payload"]["kind"], "agent_error")
        self.assertEqual(self.hc.count("send"), 1)

    def test_native_two_pack_result_still_requires_settled_and_real_registered_checks(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / "code.py").write_text("value = 0\n")
        counter = self.project / ".artifacts" / "check-count"
        check = (
            "from pathlib import Path; "
            f"p=Path({str(counter)!r}); "
            "p.write_text(str(int(p.read_text())+1 if p.exists() else 1)); "
            "from code import value; assert value == 2"
        )
        spec = {
            "task_id": "work",
            "goal": "Change value",
            "allowed_paths": ["code.py"],
            "acceptance": ["value is two"],
            "checks": [{"name": "value", "argv": [sys.executable, "-c", check], "timeout": 10}],
        }
        manager = grok_bridge.Manager.create(
            self.project / ".artifacts" / "runs" / "native-fixture",
            self.project,
            OWNER,
            runner=self.hc,
            agent="pi",
            runtime="native",
            development=spec,
            model="fixture-provider/fixture-model",
            thinking="high",
        )
        bridge = manager.bridge()
        self.hc.terminal["labels"] = {"bridge_run": bridge.data["session"]}
        self.write_json(
            bridge.root / "pi-selection.json",
            {
                "state": "ready",
                "model": "fixture-provider/fixture-model",
                "thinking": "high",
                "session_id": "a1901e11-820c-4473-aa3a-df22aeb17000",
            },
        )
        manager.submit("lead", "Fix the value", development_task="work")
        submitted = manager.bridge().task("lead")["submitted_text"]
        entries = two_pack_history(submitted)
        self.write_history(bridge, entries)
        self.hc.terminal.update(turn=1, status={"state": "done"})
        settled = {
            "state": "settled",
            "aborted": False,
            "session_id": "a1901e11-820c-4473-aa3a-df22aeb17000",
            "final_message_id": "different",
        }
        self.write_json(bridge.root / "pi-settled.json", settled)
        self.assertNotEqual(manager.observe("lead")["task"]["observed"], "evidence_ready")
        self.write_json(bridge.root / "pi-settled.json", {**settled, "final_message_id": "final"})
        self.hc.terminal["turn"] = 0
        self.assertNotEqual(manager.observe("lead")["task"]["observed"], "evidence_ready")
        self.hc.terminal["turn"] = 1
        observed = manager.observe("lead")["task"]
        self.assertEqual(observed["observed"], "evidence_ready")
        self.assertEqual(observed["evidence"]["agent_turn_id"], "original")
        self.assertFalse(observed["verified_success"])
        self.assertEqual(manager.bridge().task("lead")["evidence"]["final_message_id"], "final")

        (self.project / "code.py").write_text("value = 1\n")
        failed = manager.verify_development(expected_round="lead")
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["checks"][0]["exit_code"], 1)
        self.assertEqual(counter.read_text(), "1")
        (self.project / "code.py").write_text("value = 2\n")
        passed = manager.verify_development(expected_round="lead")
        self.assertTrue(passed["passed"])
        self.assertEqual(passed["checks"][0]["exit_code"], 0)
        self.assertEqual(counter.read_text(), "2")
        self.assertEqual(manager.status()["development"]["state"], "active")

        # A different native final after verification requires a new review/check.
        entries[-1]["id"] = "changed-final"
        self.write_history(bridge, entries)
        self.write_json(
            bridge.root / "pi-settled.json", {**settled, "final_message_id": "changed-final"}
        )
        with self.assertRaises(agent_bridge.BridgeError):
            manager.decide_development("accept", "operator", "checked previous evidence")

    def test_development_defaults_are_explicit_and_forwarded_to_pi(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        spec = {
            "task_id": "fixture",
            "goal": "Update code",
            "allowed_paths": ["code.py"],
            "acceptance": ["code works"],
            "checks": [{"name": "check", "argv": ["true"], "timeout": 1}],
        }
        obj = agent_bridge.Bridge.create(
            self.base / "development",
            self.project,
            source_home=self.source,
            runner=self.hc,
            agent="pi",
            development=spec,
        )
        self.addCleanup(shutil.rmtree, Path(obj.data["scratch"]), ignore_errors=True)
        self.assertEqual((obj.data["model"], obj.data["thinking"]), ("openai/gpt-6.1-sol", "high"))
        self.assertEqual(json.loads(obj.path.read_text())["thinking"], "high")
        obj.launch()
        command = self.hc.calls[0][0]
        self.assertIn("openai/gpt-6.1-sol", command)
        self.assertEqual(command[command.index("--thinking") + 1], "high")
        self.assertEqual(
            command[command.index("--selection-file") + 1], str(obj.root / "pi-selection.json")
        )

    def test_manager_forwards_explicit_development_model_and_thinking(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        spec = {
            "task_id": "fixture",
            "goal": "Update code",
            "allowed_paths": ["code.py"],
            "acceptance": ["code works"],
            "checks": [{"name": "check", "argv": ["true"], "timeout": 1}],
        }
        before = self.tree(self.source)
        manager = grok_bridge.Manager.create(
            self.project / ".artifacts" / "runs" / "development",
            self.project,
            OWNER,
            source_home=self.source,
            runner=self.hc,
            agent="pi",
            development=spec,
            model="provider-b/model-b",
            thinking="low",
        )
        bridge = manager.bridge()
        self.addCleanup(shutil.rmtree, Path(bridge.data["scratch"]), ignore_errors=True)
        self.assertEqual(
            (bridge.data["model"], bridge.data["thinking"]), ("provider-b/model-b", "low")
        )
        command = self.hc.calls[0][0]
        self.assertIn("provider-b/model-b", command)
        self.assertEqual(command[command.index("--thinking") + 1], "low")
        self.assertEqual(self.tree(self.source), before)

    def test_cli_thinking_option_is_forwarded_by_both_entry_points(self):
        binary = self.base / "bin"
        binary.mkdir()
        hc = binary / "hc"
        hc.write_text('#!/bin/sh\nprintf \'{"address":"fixture-terminal"}\\n\'\n')
        hc.chmod(0o700)
        env = {
            **os.environ,
            "PI_CODING_AGENT_DIR": str(self.source),
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        }
        cases = (
            ("agent_bridge.py", self.base / "cli-run", []),
            (
                "grok_bridge.py",
                self.project / ".artifacts" / "runs" / "cli-run",
                ["--bot-id", OWNER["bot_id"], "--conversation-id", OWNER["conversation_id"]],
            ),
        )
        for script, root, owner_args in cases:
            with self.subTest(script=script):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS / script),
                        "--run",
                        str(root),
                        *owner_args,
                        "start",
                        "--project",
                        str(self.project),
                        "--agent",
                        "pi",
                        "--model",
                        "provider-b/model-b",
                        "--thinking",
                        "low",
                    ],
                    text=True,
                    capture_output=True,
                    env=env,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                journal = root / ("bridge/journal.json" if owner_args else "journal.json")
                data = json.loads(journal.read_text())
                self.addCleanup(shutil.rmtree, Path(data["scratch"]), ignore_errors=True)
                self.assertEqual((data["model"], data["thinking"]), ("provider-b/model-b", "low"))

    def test_explicit_selection_must_be_confirmed_before_sending_input(self):
        self.hc.handlers["run"] = lambda args, env, timeout: {
            "address": self.hc.terminal["address"]
        }
        obj = agent_bridge.Bridge.create(
            self.base / "selection-run",
            self.project,
            model="provider-b/model-b",
            thinking="low",
            source_home=self.source,
            runner=self.hc,
            agent="pi",
        )
        self.addCleanup(shutil.rmtree, Path(obj.data["scratch"]), ignore_errors=True)
        obj.launch()
        with self.assertRaisesRegex(agent_bridge.BridgeError, "selection"):
            obj.submit("task", "First prompt")
        self.assertEqual(self.hc.count("send"), 0)
        selection = obj.root / "pi-selection.json"
        self.write_json(
            selection,
            {
                "state": "ready",
                "model": "provider-b/model-b",
                "thinking": "high",
                "session_id": "fixture-session",
            },
        )
        with self.assertRaisesRegex(agent_bridge.BridgeError, "selection"):
            obj.submit("task", "First prompt")
        self.assertEqual(self.hc.count("send"), 0)
        self.write_json(
            selection,
            {
                "state": "ready",
                "model": "provider-b/model-b",
                "thinking": "low",
                "session_id": "fixture-session",
            },
        )
        obj.submit("task", "First prompt")
        self.assertEqual(self.hc.count("send"), 1)

    def test_pending_history_reason_is_public_and_repaired_history_can_recover(self):
        manager = grok_bridge.Manager.create(
            self.project / ".artifacts" / "runs" / "diagnostics",
            self.project,
            OWNER,
            source_home=self.source,
            runner=self.hc,
            agent="pi",
            model="provider-a/model-a",
            thinking="medium",
        )
        obj = manager.bridge()
        self.addCleanup(shutil.rmtree, Path(obj.data["scratch"]), ignore_errors=True)
        self.write_json(
            obj.root / "pi-selection.json",
            {
                "state": "ready",
                "model": "provider-a/model-a",
                "thinking": "medium",
                "session_id": "pi-session-01",
            },
        )
        self.assertEqual(manager.submit("first", "Inspect only")["task"]["task_id"], "first")
        pending = manager.observe()
        self.assertEqual(pending["model_selection"]["reason"], "native_session_missing")
        self.assertFalse(pending["input_ready"])
        bad = {**session_header(self.project), "version": 4}
        self.write_history(obj, [bad])
        failed = manager.observe()
        self.assertEqual(failed["model_selection"]["reason"], "native_version_unsupported")
        self.assertEqual(failed["task"]["diagnostic"]["state"], "failed")
        self.assertFalse(failed["task"]["verified_success"])
        submitted = manager.bridge().task("first")["submitted_text"]
        self.write_history(
            obj,
            [
                session_header(self.project),
                {
                    "type": "model_change",
                    "id": "m",
                    "parentId": None,
                    "timestamp": STAMP,
                    "provider": "provider-a",
                    "modelId": "model-a",
                },
                {
                    "type": "thinking_level_change",
                    "id": "t",
                    "parentId": "m",
                    "timestamp": STAMP,
                    "thinkingLevel": "medium",
                },
                user_message("u", "t", submitted),
                assistant_message("a", "u", "first", {"summary": "done"}),
            ],
        )
        self.hc.terminal.update(turn=1, status={"state": "done"})
        recovered = manager.observe()
        self.assertEqual(recovered["model_selection"]["native_evidence"], "matched")
        self.assertEqual(recovered["task"]["observed"], "evidence_ready")
        self.assertNotIn("diagnostic", recovered["task"])
        self.assertEqual(self.hc.count("send"), 1)

    def test_manager_reports_and_gates_readiness_on_actual_pi_selection(self):
        manager = grok_bridge.Manager.create(
            self.project / ".artifacts" / "runs" / "selection-manager",
            self.project,
            OWNER,
            source_home=self.source,
            runner=self.hc,
            agent="pi",
            model="provider-b/model-b",
            thinking="low",
        )
        bridge = manager.bridge()
        self.addCleanup(shutil.rmtree, Path(bridge.data["scratch"]), ignore_errors=True)

        pending = manager.observe()
        self.assertEqual(
            pending["model_selection"],
            {
                "state": "pending",
                "scope": "startup",
                "requested": {"model": "provider-b/model-b", "thinking": "low"},
                "actual": None,
            },
        )
        self.assertFalse(pending["input_ready"])
        self.assertIsNone(pending["event_id"])
        with self.assertRaisesRegex(agent_bridge.BridgeError, "selection pending"):
            manager.submit("task", "First prompt")
        self.assertEqual(self.hc.count("send"), 0)

        self.write_json(
            bridge.root / "pi-selection.json",
            {
                "state": "ready",
                "model": "provider-b/model-b",
                "thinking": "low",
                "session_id": "fixture-session",
            },
        )
        ready = manager.observe()
        self.assertEqual(
            ready["model_selection"],
            {
                "state": "ready",
                "scope": "startup",
                "native_evidence": "pending",
                "requested": {"model": "provider-b/model-b", "thinking": "low"},
                "actual": {
                    "model": "provider-b/model-b",
                    "thinking": "low",
                    "session_id": "fixture-session",
                },
            },
        )
        self.assertTrue(ready["input_ready"])
        self.assertIsNotNone(ready["event_id"])
        manager.submit("task", "First prompt")
        self.assertEqual(self.hc.count("send"), 1)

    def test_manager_reports_selection_failure_without_sending_or_ready_event(self):
        manager = grok_bridge.Manager.create(
            self.project / ".artifacts" / "runs" / "selection-failure",
            self.project,
            OWNER,
            source_home=self.source,
            runner=self.hc,
            agent="pi",
            model="provider-b/model-b",
            thinking="low",
        )
        bridge = manager.bridge()
        self.addCleanup(shutil.rmtree, Path(bridge.data["scratch"]), ignore_errors=True)
        self.write_json(
            bridge.root / "pi-selection.json", {"state": "failed", "reason": "thinking_unsupported"}
        )

        failed = manager.observe()
        self.assertEqual(failed["model_selection"]["state"], "failed")
        self.assertEqual(failed["model_selection"]["reason"], "thinking_unsupported")
        self.assertIsNone(failed["model_selection"]["actual"])
        self.assertFalse(failed["input_ready"])
        event = manager.outbox().get(failed["event_id"])
        self.assertEqual(event["payload"]["kind"], "agent_error")
        with self.assertRaisesRegex(
            agent_bridge.BridgeError, "selection failed: thinking_unsupported"
        ):
            manager.submit("task", "First prompt")
        self.assertEqual(self.hc.count("send"), 0)

    def test_native_evidence_must_belong_to_startup_selection_session(self):
        manager = grok_bridge.Manager.create(
            self.project / ".artifacts" / "runs" / "native-mismatch",
            self.project,
            OWNER,
            source_home=self.source,
            runner=self.hc,
            agent="pi",
            model="provider-b/model-b",
            thinking="low",
        )
        bridge = manager.bridge()
        self.addCleanup(shutil.rmtree, Path(bridge.data["scratch"]), ignore_errors=True)
        self.write_json(
            bridge.root / "pi-selection.json",
            {
                "state": "ready",
                "model": "provider-b/model-b",
                "thinking": "low",
                "session_id": "selected-session",
            },
        )
        manager.submit("task", "First prompt")
        task = manager.bridge().task("task")
        self.write_history(
            bridge,
            [
                session_header(self.project),
                user_message("u1", None, task["submitted_text"]),
                assistant_message("a1", "u1", "task", {"ok": True}),
            ],
        )
        self.hc.terminal.update(turn=1, status={"state": "done"})

        result = manager.observe()
        self.assertEqual(result["model_selection"]["state"], "failed")
        self.assertEqual(result["model_selection"]["reason"], "native_session_mismatch")
        self.assertFalse(result["input_ready"])
        self.assertEqual(result["task"]["observed"], "unknown")
        self.assertIsNone(result["task"]["evidence"]["result"])
        event = manager.outbox().get(result["event_id"])
        self.assertEqual(
            (event["payload"]["kind"], event["payload"]["task_id"]), ("agent_error", "task")
        )

    def test_native_model_drift_is_reported_and_does_not_certify_result(self):
        manager = grok_bridge.Manager.create(
            self.project / ".artifacts" / "runs" / "model-drift",
            self.project,
            OWNER,
            source_home=self.source,
            runner=self.hc,
            agent="pi",
            model="provider-b/model-b",
            thinking="low",
        )
        bridge = manager.bridge()
        self.addCleanup(shutil.rmtree, Path(bridge.data["scratch"]), ignore_errors=True)
        self.write_json(
            bridge.root / "pi-selection.json",
            {
                "state": "ready",
                "model": "provider-b/model-b",
                "thinking": "low",
                "session_id": "pi-session-01",
            },
        )
        manager.submit("task", "First prompt")
        task = manager.bridge().task("task")
        model = {
            "type": "model_change",
            "id": "m1",
            "parentId": None,
            "timestamp": STAMP,
            "provider": "provider-b",
            "modelId": "model-b",
        }
        thinking = {
            "type": "thinking_level_change",
            "id": "t1",
            "parentId": "m1",
            "timestamp": STAMP,
            "thinkingLevel": "low",
        }
        drift = {
            "type": "model_change",
            "id": "m2",
            "parentId": "t1",
            "timestamp": STAMP,
            "provider": "provider-a",
            "modelId": "model-a",
        }
        self.write_history(
            bridge,
            [
                session_header(self.project),
                model,
                thinking,
                drift,
                user_message("u1", "m2", task["submitted_text"]),
                assistant_message("a1", "u1", "task", {"ok": True}),
            ],
        )
        self.hc.terminal.update(turn=1, status={"state": "done"})

        result = manager.observe()
        self.assertEqual(result["model_selection"]["reason"], "native_selection_mismatch")
        self.assertFalse(result["input_ready"])
        self.assertIsNone(result["task"]["evidence"]["result"])

    def test_closed_run_retains_validated_native_selection_evidence(self):
        manager = grok_bridge.Manager.create(
            self.project / ".artifacts" / "runs" / "closed-selection",
            self.project,
            OWNER,
            source_home=self.source,
            runner=self.hc,
            agent="pi",
            model="provider-b/model-b",
            thinking="low",
        )
        bridge = manager.bridge()
        self.addCleanup(shutil.rmtree, Path(bridge.data["scratch"]), ignore_errors=True)
        self.write_json(
            bridge.root / "pi-selection.json",
            {
                "state": "ready",
                "model": "provider-b/model-b",
                "thinking": "low",
                "session_id": "pi-session-01",
            },
        )
        manager.submit("task", "First prompt")
        task = manager.bridge().task("task")
        model = {
            "type": "model_change",
            "id": "m1",
            "parentId": None,
            "timestamp": STAMP,
            "provider": "provider-b",
            "modelId": "model-b",
        }
        thinking = {
            "type": "thinking_level_change",
            "id": "t1",
            "parentId": "m1",
            "timestamp": STAMP,
            "thinkingLevel": "low",
        }
        self.write_history(
            bridge,
            [
                session_header(self.project),
                model,
                thinking,
                user_message("u1", "t1", task["submitted_text"]),
                assistant_message(
                    "a1", "u1", "task", {"ok": True}, provider="provider-b", model="model-b"
                ),
            ],
        )
        self.hc.terminal.update(turn=1, status={"state": "done"})
        before = manager.observe()
        self.assertEqual(before["task"]["observed"], "evidence_ready")
        self.assertEqual(before["model_selection"]["native_evidence"], "matched")
        manager.close()
        after = manager.status()
        self.assertTrue(after["closed"])
        self.assertFalse(after["input_ready"])
        self.assertEqual(after["model_selection"], before["model_selection"])

    def test_pi_launch_is_read_only_isolated_and_uses_saved_default(self):
        source_before = self.tree(self.source)
        obj = self.make_bridge()
        self.assertEqual(obj.data["agent"], "pi")
        self.assertEqual(obj.data["model"], "provider-a/model-a")
        scratch = Path(obj.data["scratch"])
        pi_home = scratch / "pi"
        self.assertEqual(
            json.loads((pi_home / "settings.json").read_text()),
            {
                "defaultProvider": "provider-a",
                "defaultModel": "model-a",
                "defaultThinkingLevel": "medium",
                "lastChangelogVersion": "fixture-version",
                "compaction": {"enabled": False},
                "retry": {"enabled": False, "provider": {"maxRetries": 0}},
                "quietStartup": True,
            },
        )
        self.assertEqual(
            json.loads((pi_home / "models.json").read_text()),
            {"providers": {"provider-a": self.models["providers"]["provider-a"]}},
        )
        self.assertEqual(
            json.loads((pi_home / "auth.json").read_text()), {"provider-a": self.auth["provider-a"]}
        )
        self.assertEqual(set(self.tree(pi_home)), {"settings.json", "models.json", "auth.json"})
        for name in ("settings.json", "models.json", "auth.json"):
            self.assertEqual(stat.S_IMODE((pi_home / name).stat().st_mode), 0o600)

        def launch(args, env, timeout):
            on_disk = json.loads(obj.path.read_text())
            self.assertEqual(on_disk["agent"], "pi")
            self.assertEqual(on_disk["launch"], "delivery_unknown")
            self.assertIsNone(on_disk["address"])
            command = args[args.index("pi") :]
            self.assertNotIn("codex", command)
            for flag in (
                "--no-approve",
                "--no-extensions",
                "--no-mcp",
                "--no-skills",
                "--no-prompt-templates",
                "--no-themes",
            ):
                self.assertIn(flag, command)
            self.assertEqual(command[command.index("--tools") + 1], "read,grep,find,ls")
            self.assertEqual(command[command.index("--model") + 1], "provider-a/model-a")
            self.assertEqual(command[command.index("--session-dir") + 1], str(pi_home / "sessions"))
            self.assertEqual(env["PI_CODING_AGENT_DIR"], str(pi_home))
            self.assertEqual(env["PI_CODING_AGENT_SESSION_DIR"], str(pi_home / "sessions"))
            self.assertEqual(env["PI_PROGRAM_STATUS"], "1")
            self.assertEqual(env["HC_SESSION"], on_disk["session"])
            for key in (
                "HC_FIXTURE_PARENT",
                "PI_FIXTURE_PARENT",
                "CODEX_SESSION_ID",
                "CODEX_THREAD_ID",
                "CODEX_TOOL_CALL_ID",
                "CODEX_CI",
                "NODE_OPTIONS",
                "NODE_PATH",
                "NODE_REPL_EXTERNAL_MODULE",
                "NODE_V8_COVERAGE",
            ):
                self.assertNotIn(key, env)
            for key, name in (
                ("XDG_CONFIG_HOME", "config"),
                ("XDG_STATE_HOME", "state"),
                ("XDG_RUNTIME_DIR", "run"),
                ("XDG_CACHE_HOME", "cache"),
            ):
                self.assertEqual(env[key], str(scratch / name))
            return {"address": self.hc.terminal["address"]}

        self.hc.handlers["run"] = launch
        inherited = {
            key: "must-not-inherit"
            for key in (
                "HC_FIXTURE_PARENT",
                "PI_FIXTURE_PARENT",
                "CODEX_SESSION_ID",
                "CODEX_THREAD_ID",
                "CODEX_TOOL_CALL_ID",
                "CODEX_CI",
                "NODE_OPTIONS",
                "NODE_PATH",
                "NODE_REPL_EXTERNAL_MODULE",
                "NODE_V8_COVERAGE",
            )
        }
        with patch.dict(os.environ, inherited):
            obj.launch()
        self.assertEqual(self.tree(self.source), source_before)
        self.assertEqual(self.hc.count("run"), 1)

    def test_explicit_model_selects_only_its_provider_and_cleanup_removes_credentials(self):
        before = self.tree(self.source)
        obj = self.make_bridge(model="provider-b/model-b")
        home = Path(obj.data["scratch"]) / "pi"
        self.assertEqual(obj.data["model"], "provider-b/model-b")
        self.assertEqual(obj.data["pi_auth_mode"], "isolated_api_key")
        self.assertFalse((home / "auth-source.json").exists())
        self.assertEqual(
            json.loads((home / "models.json").read_text()),
            {"providers": {"provider-b": self.models["providers"]["provider-b"]}},
        )
        self.assertEqual(
            json.loads((home / "auth.json").read_text()), {"provider-b": self.auth["provider-b"]}
        )
        settings = json.loads((home / "settings.json").read_text())
        self.assertEqual(
            (settings["defaultProvider"], settings["defaultModel"]), ("provider-b", "model-b")
        )
        obj.launch()
        command = self.hc.calls[0][0]
        self.assertEqual(command[command.index("--model") + 1], "provider-b/model-b")
        scratch = Path(obj.data["scratch"])
        self.assertTrue(all(record["passed"] for record in obj.close()))
        self.assertTrue(json.loads(obj.path.read_text())["closed"])
        self.assertFalse(scratch.exists())
        self.assertEqual(self.tree(self.source), before)

    def test_network_settings_are_copied_privately_without_unrelated_settings(self):
        network = {
            "httpProxy": "http://fixture-user:fixture-secret@proxy.invalid:3128",
            "httpIdleTimeoutMs": 23000,
            "websocketConnectTimeoutMs": 17000,
            "transport": "sse",
        }
        self.write_json(self.source / "settings.json", {**self.settings, **network})
        before = self.tree(self.source)
        obj = self.make_bridge()
        settings_path = Path(obj.data["scratch"]) / "pi" / "settings.json"
        selected = json.loads(settings_path.read_text())
        self.assertEqual({key: selected[key] for key in network}, network)
        self.assertTrue(
            {"packages", "extensions", "mcpServers", "trustedProjects", "theme"}.isdisjoint(
                selected
            )
        )
        self.assertEqual(stat.S_IMODE(settings_path.stat().st_mode), 0o600)
        self.assertEqual(self.tree(self.source), before)

    def test_saved_model_id_with_slash_keeps_its_saved_provider(self):
        settings = {**self.settings, "defaultProvider": "openrouter", "defaultModel": "qwen/model"}
        models = {
            "providers": {
                "openrouter": {
                    "apiKey": "PRIVATE-OPENROUTER",
                    "models": [{"id": "qwen/model", "name": "Fixture model"}],
                },
                "qwen": {"apiKey": "PRIVATE-UNRELATED"},
            }
        }
        auth = {
            "openrouter": {"type": "api_key", "key": "PRIVATE-OPENROUTER-AUTH"},
            "qwen": {"type": "api_key", "key": "PRIVATE-UNRELATED-AUTH"},
        }
        self.write_json(self.source / "settings.json", settings)
        self.write_json(self.source / "models.json", models)
        self.write_json(self.source / "auth.json", auth)
        before = self.tree(self.source)
        obj = self.make_bridge()
        home = Path(obj.data["scratch"]) / "pi"
        saved = json.loads((home / "settings.json").read_text())
        self.assertEqual(
            (saved["defaultProvider"], saved["defaultModel"]), ("openrouter", "qwen/model")
        )
        self.assertEqual(obj.data["model"], "openrouter/qwen/model")
        self.assertEqual(
            json.loads((home / "models.json").read_text()),
            {"providers": {"openrouter": models["providers"]["openrouter"]}},
        )
        self.assertEqual(
            json.loads((home / "auth.json").read_text()), {"openrouter": auth["openrouter"]}
        )
        obj.launch()
        command = self.hc.calls[0][0]
        self.assertEqual(command[command.index("--model") + 1], "openrouter/qwen/model")
        self.assertEqual(self.tree(self.source), before)

    def test_bom_configuration_and_commented_models_preserve_quoted_values(self):
        selected = copy.deepcopy(self.models["providers"]["provider-a"])
        selected["baseUrl"] = "https://provider-a.invalid/api//path"
        selected["headers"] = {
            "X-Fixture": r"escaped quote: \" then // and /* remain */",
            "X-Markers": 'literal "quoted" // and /* markers */',
        }
        model_text = (
            '{\n // registry comment\n "providers": {\n'
            ' /* comment before selected provider */ "provider-a": '
            + json.dumps(selected)
            + ", // trailing provider comment\n"
            ' "provider-b": ' + json.dumps(self.models["providers"]["provider-b"]) + "\n }\n}\n"
        )
        for name, text in (
            ("settings.json", json.dumps(self.settings)),
            ("auth.json", json.dumps(self.auth)),
            ("models.json", model_text),
        ):
            (self.source / name).write_text(text, encoding="utf-8-sig")
            self.assertTrue((self.source / name).read_bytes().startswith(b"\xef\xbb\xbf"))
        before = self.tree(self.source)
        obj = self.make_bridge()
        home = Path(obj.data["scratch"]) / "pi"
        settings = json.loads((home / "settings.json").read_text())
        self.assertEqual(
            (settings["defaultProvider"], settings["defaultModel"]), ("provider-a", "model-a")
        )
        self.assertEqual(
            json.loads((home / "auth.json").read_text()), {"provider-a": self.auth["provider-a"]}
        )
        self.assertEqual(
            json.loads((home / "models.json").read_text()), {"providers": {"provider-a": selected}}
        )
        obj.launch()
        self.assertEqual(self.hc.count("run"), 1)
        self.assertEqual(self.tree(self.source), before)

    def test_selected_oauth_uses_native_source_without_copy_and_preserves_it_on_cleanup(self):
        credential = {
            "type": "oauth",
            "access": "PRIVATE-OAUTH-ACCESS",
            "refresh": "PRIVATE-OAUTH-REFRESH",
            "expires": 123456789,
        }
        self.write_json(self.source / "auth.json", {**self.auth, "provider-a": credential})
        before = self.tree(self.source)
        obj = self.make_bridge()
        scratch = Path(obj.data["scratch"])
        home = scratch / "pi"
        pointer = home / "auth-source.json"
        self.assertEqual(obj.data["pi_auth_mode"], "native_shared")
        self.assertEqual(json.loads(obj.path.read_text())["pi_auth_mode"], "native_shared")
        self.assertEqual(
            json.loads(pointer.read_text()), {"path": str((self.source / "auth.json").resolve())}
        )
        self.assertEqual(stat.S_IMODE(pointer.stat().st_mode), 0o600)
        self.assertFalse((home / "auth.json").exists())
        for content in self.tree(home).values():
            self.assertNotIn(b"PRIVATE-OAUTH-ACCESS", content)
            self.assertNotIn(b"PRIVATE-OAUTH-REFRESH", content)
        self.assertEqual(self.tree(self.source), before)

        def launch(args, env, timeout):
            journal = json.loads(obj.path.read_text())
            self.assertEqual(
                (journal["pi_auth_mode"], journal["launch"]), ("native_shared", "delivery_unknown")
            )
            command = args[args.index("node") :]
            self.assertEqual(
                command,
                [
                    "node",
                    str(resource_path("pi_oauth_session.mjs")),
                    "/fixture/bin/pi",
                    str(home),
                    str(self.project),
                    "provider-a/model-a",
                ],
            )
            self.assertEqual(env["PI_CODING_AGENT_DIR"], str(home))
            self.assertEqual(env["PI_CODING_AGENT_SESSION_DIR"], str(home / "sessions"))
            return {"address": self.hc.terminal["address"]}

        self.hc.handlers["run"] = launch
        with patch.object(agent_bridge.shutil, "which", return_value="/fixture/bin/pi"):
            obj.launch()
        self.assertEqual(self.hc.count("run"), 1)
        self.assertEqual(self.tree(self.source), before)
        self.assertTrue(all(record["passed"] for record in obj.close()))
        self.assertTrue(json.loads(obj.path.read_text())["closed"])
        self.assertFalse(pointer.exists())
        self.assertFalse(scratch.exists())
        self.assertEqual(self.tree(self.source), before)

    def test_malformed_oauth_or_unknown_auth_is_rejected_without_mutation_or_hc(self):
        oauth = {
            "type": "oauth",
            "access": "PRIVATE-ACCESS",
            "refresh": "PRIVATE-REFRESH",
            "expires": 123456789,
        }
        credentials = {
            "missing_access": {key: value for key, value in oauth.items() if key != "access"},
            "empty_access": {**oauth, "access": ""},
            "non_string_access": {**oauth, "access": 123},
            "empty_refresh": {**oauth, "refresh": ""},
            "non_string_refresh": {**oauth, "refresh": None},
            "missing_expiry": {key: value for key, value in oauth.items() if key != "expires"},
            "string_expiry": {**oauth, "expires": "123456789"},
            "boolean_expiry": {**oauth, "expires": True},
            "infinite_expiry": {**oauth, "expires": float("inf")},
            "nan_expiry": {**oauth, "expires": float("nan")},
            "unknown": {"type": "unknown", "key": "PRIVATE-OTHER"},
            "missing_type": {"key": "PRIVATE-MISSING-TYPE"},
            "not_a_dict": "PRIVATE-STRING",
        }
        for label, credential in credentials.items():
            with self.subTest(credential=label):
                auth = {**self.auth, "provider-a": credential}
                self.write_json(self.source / "auth.json", auth)
                before = self.tree(self.source)
                root = self.base / ("refused-auth-" + label)
                with self.assertRaises(agent_bridge.BridgeError) as error:
                    agent_bridge.Bridge.create(
                        root, self.project, source_home=self.source, runner=self.hc, agent="pi"
                    )
                self.assertNotIn("PRIVATE-", str(error.exception))
                journal = json.loads((root / "journal.json").read_text())
                self.assertTrue(journal["closed"])
                self.assertEqual(journal["launch"], "configuration_failed")
                self.assertFalse(Path(journal["scratch"]).exists())
                self.assertEqual(self.tree(self.source), before)
                self.assertEqual(self.hc.calls, [])

    def test_shared_oauth_rejects_shell_api_keys_from_any_provider_before_hc(self):
        oauth = {
            "type": "oauth",
            "access": "PRIVATE-ACCESS",
            "refresh": "PRIVATE-REFRESH",
            "expires": 123456789,
        }
        api_keys = {
            "provider-b": {"type": "api_key", "key": " !unsafe"},
            "provider-c": {"type": "api_key", "key": "!unsafe"},
        }
        for provider, credential in api_keys.items():
            with self.subTest(provider=provider):
                auth = {**self.auth, "provider-a": oauth, provider: credential}
                self.write_json(self.source / "auth.json", auth)
                before = self.tree(self.source)
                root = self.base / ("refused-shell-" + provider)
                with self.assertRaises(agent_bridge.BridgeError) as error:
                    agent_bridge.Bridge.create(
                        root, self.project, source_home=self.source, runner=self.hc, agent="pi"
                    )
                self.assertNotIn("PRIVATE-", str(error.exception))
                journal = json.loads((root / "journal.json").read_text())
                self.assertTrue(journal["closed"])
                self.assertEqual(journal["launch"], "configuration_failed")
                self.assertFalse(Path(journal["scratch"]).exists())
                self.assertEqual(self.tree(self.source), before)
                self.assertEqual(self.hc.calls, [])

    def test_unselected_oauth_is_not_imported_or_a_launch_blocker(self):
        auth = {
            **self.auth,
            "provider-b": {
                "type": "oauth",
                "access": "PRIVATE-ACCESS",
                "refresh": "PRIVATE-REFRESH",
                "expires": 123456789,
            },
        }
        self.write_json(self.source / "auth.json", auth)
        before = self.tree(self.source)
        obj = self.make_bridge()
        home = Path(obj.data["scratch"]) / "pi"
        self.assertEqual(
            json.loads((home / "auth.json").read_text()), {"provider-a": self.auth["provider-a"]}
        )
        obj.launch()
        self.assertEqual(self.hc.count("run"), 1)
        self.assertEqual(self.tree(self.source), before)

    def test_shell_derived_selected_provider_credentials_are_rejected_without_hc(self):
        for where in ("apiKey", "headers", "model_headers", "model_override_headers", "auth"):
            with self.subTest(where=where):
                models, auth = copy.deepcopy(self.models), copy.deepcopy(self.auth)
                if where == "auth":
                    auth["provider-a"]["key"] = " !echo dangerous"
                elif where == "headers":
                    models["providers"]["provider-a"]["headers"] = {"Authorization": "!unsafe"}
                elif where == "model_headers":
                    models["providers"]["provider-a"]["models"][0]["headers"] = {
                        "Authorization": "!unsafe"
                    }
                elif where == "model_override_headers":
                    models["providers"]["provider-a"]["modelOverrides"] = {
                        "model-a": {"headers": {"Authorization": "!unsafe"}}
                    }
                else:
                    models["providers"]["provider-a"]["apiKey"] = "!unsafe"
                self.write_json(self.source / "models.json", models)
                self.write_json(self.source / "auth.json", auth)
                root = self.base / ("refused-" + where)
                with self.assertRaises(agent_bridge.BridgeError):
                    agent_bridge.Bridge.create(
                        root, self.project, source_home=self.source, runner=self.hc, agent="pi"
                    )
                journal = json.loads((root / "journal.json").read_text())
                self.assertTrue(journal["closed"])
                self.assertFalse(Path(journal["scratch"]).exists())
                self.assertEqual(journal["launch"], "configuration_failed")
                self.assertEqual(self.hc.calls, [])

    def test_lost_launch_receipt_is_durable_and_never_replayed(self):
        obj = self.make_bridge()

        def lost(args, env, timeout):
            journal = json.loads(obj.path.read_text())
            self.assertEqual(journal["agent"], "pi")
            self.assertEqual(journal["launch"], "delivery_unknown")
            raise subprocess.TimeoutExpired("fake hc run", timeout)

        self.hc.handlers["run"] = lost
        with self.assertRaises(subprocess.TimeoutExpired):
            obj.launch()
        reopened = agent_bridge.Bridge(obj.root, runner=self.hc)
        with self.assertRaises(agent_bridge.BridgeError):
            reopened.launch()
        self.assertEqual(self.hc.count("run"), 1)

    def test_lost_send_is_reconciled_by_native_pi_evidence_without_resending(self):
        obj = self.make_bridge()
        obj.launch()

        def lost(args, env, timeout):
            task = json.loads(obj.path.read_text())["tasks"][0]
            self.assertEqual(task["id"], "first")
            self.assertEqual(task["delivery"], "unknown")
            self.assertEqual(task["submitted_text"], args[-1])
            self.assertEqual(task["input_sha256"], agent_bridge.digest(args[-1]))
            self.assertIn(self.hc.terminal["address"], args[:-1])
            raise subprocess.TimeoutExpired("fake hc send", timeout)

        self.hc.handlers["send"] = lost
        task = obj.submit("first", "PRIVATE-FIXTURE-PROMPT")
        self.assertEqual(task["delivery"], "unknown")
        reopened = agent_bridge.Bridge(obj.root, runner=self.hc)
        with self.assertRaises(agent_bridge.BridgeError):
            reopened.submit("first", "PRIVATE-FIXTURE-PROMPT")
        with self.assertRaises(agent_bridge.BridgeError):
            reopened.submit("second", "Follow up", parent="first")
        self.write_history(
            reopened,
            [
                session_header(self.project),
                user_message("native-user-1", None, task["submitted_text"]),
                assistant_message(
                    "native-answer-1", "native-user-1", "first", {"value": "fixture"}
                ),
            ],
        )
        self.hc.terminal["turn"] = 1
        self.hc.terminal["status"]["state"] = "done"
        observed = reopened.observe("first")
        self.assertEqual(observed["delivery"], "received")
        self.assertEqual(observed["observed"], "evidence_ready")
        self.assertEqual(observed["evidence"]["thread_id"], "pi-session-01")
        self.assertEqual(observed["evidence"]["agent_turn_id"], "native-user-1")
        self.assertTrue(observed["evidence"]["agent_completed"])
        self.assertFalse(observed["verified_success"])
        self.assertEqual(self.hc.count("send"), 1)

    def test_blocked_pi_terminal_refuses_ordinary_input(self):
        obj = self.make_bridge()
        obj.launch()
        self.hc.terminal["status"]["state"] = "blocked"
        with self.assertRaises(agent_bridge.BridgeError):
            obj.submit("first", "Do not send")
        self.assertEqual(self.hc.count("send"), 0)
        self.assertEqual(json.loads(obj.path.read_text())["tasks"], [])

    def test_manager_forwards_pi_keeps_public_output_private_and_verifies_two_turns(self):
        root = self.project / ".artifacts" / "runs" / "fixture-run"

        def launched(args, env, timeout):
            manager = json.loads((root / "manager.json").read_text())
            journal = json.loads((root / "bridge" / "journal.json").read_text())
            self.assertEqual((manager["agent"], manager["setup"]), ("pi", "creation_unknown"))
            self.assertEqual((journal["agent"], journal["launch"]), ("pi", "delivery_unknown"))
            self.assertIn("pi", args)
            return {"address": self.hc.terminal["address"]}

        self.hc.handlers["run"] = launched
        manager = self.make_manager()
        bridge = manager.bridge()
        first = manager.submit("question", "PRIVATE-FIXTURE-PROMPT")
        first_task = manager.bridge().task("question")
        events = [
            session_header(self.project),
            user_message("native-user-1", None, first_task["submitted_text"]),
            assistant_message(
                "native-answer-1",
                "native-user-1",
                "question",
                {"question": "Which language should I use?"},
                kind="question",
            ),
        ]
        self.write_history(bridge, events)
        self.hc.terminal.update(turn=1, status={"state": "done"})
        observed_first = manager.observe("question")
        self.assertEqual(observed_first["agent"], "pi")
        self.assertEqual(observed_first["task"]["observed"], "needs_human")
        self.assertEqual(observed_first["task"]["evidence"]["thread_id"], "pi-session-01")
        self.assertEqual(observed_first["task"]["evidence"]["agent_turn_id"], "native-user-1")
        question_check = manager.verify("question", "question")
        self.assertTrue(question_check["passed"], question_check["checks"])
        self.assertFalse(question_check["task"]["verified_success"])

        second = manager.submit("clarification", "Use English.", parent="question")
        second_task = manager.bridge().task("clarification")
        events.extend(
            [
                user_message("native-user-2", "native-answer-1", second_task["submitted_text"]),
                assistant_message(
                    "native-answer-2",
                    "native-user-2",
                    "clarification",
                    {
                        "language": "English",
                        "previous_question_task_id": "question",
                        "done_verifies_success": False,
                        "explanation": "Check the evidence independently.",
                    },
                ),
            ]
        )
        self.write_history(bridge, events)
        self.hc.terminal["turn"] = 2
        observed_second = manager.observe("clarification")
        self.assertEqual(observed_second["task"]["observed"], "evidence_ready")
        self.assertEqual(observed_second["task"]["evidence"]["thread_id"], "pi-session-01")
        self.assertEqual(observed_second["task"]["evidence"]["agent_turn_id"], "native-user-2")
        self.assertFalse(observed_second["task"]["verified_success"])
        checked = manager.verify("clarification", "clarification")
        self.assertTrue(checked["passed"], checked["checks"])
        self.assertTrue(checked["task"]["verified_success"])
        self.assertTrue(manager.status("clarification")["task"]["verified_success"])
        self.assertEqual(self.hc.count("send"), 2)
        public = json.dumps(
            [
                first,
                observed_first,
                question_check,
                second,
                observed_second,
                checked,
                manager.status("clarification"),
            ]
        )
        for private in (
            "PRIVATE-FIXTURE-PROMPT",
            "PRIVATE-FIXTURE-SCREEN",
            "PRIVATE-FIXTURE-A-AUTH",
            "PRIVATE-FIXTURE-B-AUTH",
            "PRIVATE-FIXTURE-A-MODEL-KEY",
            "PRIVATE-FIXTURE-B-MODEL-KEY",
            str(self.source),
            str(bridge.data["scratch"]),
            "submitted_text",
            "input_sha256",
            "final_message_id",
        ):
            self.assertNotIn(private, public)
        scratch = Path(bridge.data["scratch"])
        self.assertTrue(manager.close()["closed"])
        self.assertFalse(scratch.exists())

    def test_probe_selects_pi_without_requiring_codex_or_user_environment(self):
        for relative in readonly_contracts.REQUIRED_FILES:
            target = self.project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("disposable fixture\n")
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            self.assertEqual(kwargs["cwd"], kwargs["env"]["HOME"])
            self.assertEqual(kwargs["env"]["PI_CODING_AGENT_DIR"], str(Path(kwargs["cwd"]) / "pi"))
            self.assertNotIn("HC_FIXTURE_PARENT", kwargs["env"])
            self.assertNotIn("PI_FIXTURE_PARENT", kwargs["env"])
            self.assertNotIn("CODEX_THREAD_ID", kwargs["env"])
            return SimpleNamespace(returncode=0, stdout=args[0] + " 1.0\n", stderr="")

        with patch.dict(
            os.environ,
            {
                "HC_FIXTURE_PARENT": "private",
                "PI_FIXTURE_PARENT": "private",
                "CODEX_THREAD_ID": "private",
            },
        ):
            result = readiness.probe(self.project, runner=runner, agent="pi")
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["agent"], "pi")
        self.assertEqual([args for args, _ in calls], [["hc", "--version"], ["pi", "--version"]])
        self.assertFalse(Path(calls[0][1]["cwd"]).exists())
        self.assertIn("coding agent authentication", result["not_verified"])

    def test_unknown_agent_is_rejected_before_resources_or_hc(self):
        root = self.base / "unsupported-bridge"
        with self.assertRaises(agent_bridge.BridgeError):
            agent_bridge.Bridge.create(
                root, self.project, source_home=self.source, runner=self.hc, agent="unknown"
            )
        self.assertFalse(root.exists())
        root = self.project / ".artifacts" / "runs" / "unsupported-manager"
        with self.assertRaises(agent_bridge.BridgeError):
            grok_bridge.Manager.create(
                root, self.project, OWNER, source_home=self.source, runner=self.hc, agent="unknown"
            )
        self.assertFalse(root.exists())
        with self.assertRaises(ValueError):
            readiness.probe(self.project, agent="unknown")
        self.assertEqual(self.hc.calls, [])

    def test_legacy_journal_without_agent_remains_codex(self):
        empty_source = self.base / "source-codex"
        empty_source.mkdir()
        obj = agent_bridge.Bridge.create(
            self.base / "legacy-run", self.project, source_home=empty_source, runner=self.hc
        )
        self.addCleanup(shutil.rmtree, Path(obj.data["scratch"]), ignore_errors=True)
        del obj.data["agent"]
        obj.persist()
        reopened = agent_bridge.Bridge(obj.root, runner=self.hc)
        self.assertEqual(reopened.agent(), "codex")
        reopened.launch()
        args = self.hc.calls[0][0]
        self.assertIn("codex", args)
        self.assertNotIn("pi", args)
        self.assertEqual(reopened.data["model"], "gpt-6.1-terra")


if __name__ == "__main__":
    unittest.main()
