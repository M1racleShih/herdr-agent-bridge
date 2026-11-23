"""Manager contracts with disposable source trees and fake hc; no agents/network."""

import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from herdr_agent_bridge import grok_bridge as manager_module
from herdr_agent_bridge.agent_bridge import BridgeError, HcError, save
from herdr_agent_bridge.project_bridge import Project

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

OWNER = {"bot_id": "test-bot", "conversation_id": "test-conversation"}


class FakeHC:
    def __init__(self):
        self.calls = []
        self.terminal = {
            "address": "own-terminal",
            "state": "live",
            "turn": 0,
            "status": {"state": "idle"},
            "created_ms": 0,
        }
        self.handlers = {}

    def __call__(self, args, env, timeout):
        self.calls.append(list(args))
        command = args[0]
        if command in self.handlers:
            return self.handlers[command](args, env, timeout)
        if command == "run":
            return {"address": self.terminal["address"]}
        if command == "get":
            if self.terminal is None:
                raise HcError({"stderr": json.dumps({"error": {"kind": "not_found"}})})
            return copy.deepcopy(self.terminal)
        if command == "wait":
            return {"reason": "timeout"}
        if command == "send":
            return {"started": True}
        if command == "read":
            return {"text": "PRIVATE NATIVE TRANSCRIPT"}
        if command == "kill":
            self.terminal["state"] = "finished"
        if command == "rm":
            self.terminal = None
        if command == "ls":
            return {"terminals": [self.terminal] if self.terminal else []}
        return {}

    def count(self, command):
        return sum(args[0] == command for args in self.calls)


class ManagerChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-grok-manager-test-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        for relative in ("src/herdr_agent_bridge/tools/smoke_hc.py", "tests/test_smoke_hc.py"):
            dest = self.project / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SCRIPTS.parent / relative, dest)
        self.source = Path(self.temp.name) / "empty-source-home"
        self.source.mkdir()
        self.hc = FakeHC()
        self.root = self.project / ".artifacts" / "runs" / "test-run"
        self.manager = manager_module.Manager.create(
            self.root, self.project, OWNER, source_home=self.source, runner=self.hc
        )
        self.addCleanup(self.cleanup)
        self.events = [{"type": "session_meta", "payload": {"id": "native-thread"}}]

    def cleanup(self):
        # Every test owns exactly its temporary source home and fake hc state.
        bridge = self.manager.bridge()
        scratch = Path(bridge.data["scratch"])
        self.manager.close()
        if scratch.exists():
            shutil.rmtree(scratch)

    def config(self):
        path = Path(self.temp.name) / "webhook.json"
        save(path, {**OWNER, "url": "https://example.invalid/routine", "key": "PRIVATE-BEARER"})
        return path

    def test_public_descendant_check_field_stays_fixed_and_checks_credentials(self):
        project = object.__new__(Project)
        project.path = self.project / "config.json"
        project.data = {
            "project_root": str(self.project),
            "webhook_config": None,
            "owner": {"bot_id": "left", "conversation_id": "fixture"},
        }
        project.credential = project.endpoint = project.relay_home = None
        self.assertEqual(project.public({"left_descendants": False}), {"left_descendants": False})
        project.credential = "left_descendants"
        with self.assertRaises(BridgeError):
            project.public({"left_descendants": False})

    def finish(self, task_id, answer, kind="result"):
        bridge = self.manager.bridge()
        task = bridge.task(task_id)
        turn = "native-" + task_id
        self.events.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "role": "user",
                        "content": [{"text": task["submitted_text"]}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": turn},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [
                            {
                                "text": json.dumps(
                                    {"task_id": task_id, "kind": kind, "answer": answer}
                                )
                            }
                        ],
                        "internal_chat_message_metadata_passthrough": {"turn_id": turn},
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}},
            ]
        )
        path = Path(bridge.data["scratch"]) / "codex" / "sessions" / "rollout.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(event) + "\n" for event in self.events))
        self.hc.terminal["turn"] += 1
        self.hc.terminal["status"]["state"] = "done"
        return self.manager.observe(task_id)

    def analysis_answer(self):
        sources = []
        for relative in ("src/herdr_agent_bridge/tools/smoke_hc.py", "tests/test_smoke_hc.py"):
            line, quote = next(
                (i, line)
                for i, line in enumerate((self.project / relative).read_text().splitlines(), 1)
                if len(line.strip()) >= 8
            )
            sources.append({"path": relative, "line": line, "quote": quote})
        return {
            "initial_task_ids": ["task-001", "followup-001"],
            "blocked_error_kind": "blocked",
            "regression_test_names": [
                "test_optimized_run_rejects_wrong_task_evidence",
                "test_sigterm_during_cleanup_cannot_report_success",
                "test_sigterm_reports_failure_and_attempts_cleanup",
            ],
            "success_requires_cleanup": True,
            "limitation": "This local fixture does not prove real agent or Grok integration.",
            "fixture_proves_real_agent": False,
            "fixture_proves_grok": False,
            "sources": sources,
        }

    def test_owner_mismatch_refuses_every_operation_before_hc(self):
        before = len(self.hc.calls)
        for owner in ({**OWNER, "bot_id": "other"}, {**OWNER, "conversation_id": "other"}):
            with self.subTest(owner=owner), self.assertRaises(BridgeError):
                manager_module.Manager(self.root, owner, runner=self.hc)
        self.assertEqual(len(self.hc.calls), before)

    def test_credential_owner_is_rejected_before_creating_run_or_calling_hc(self):
        key = "PRIVATE-BEARER"
        before = len(self.hc.calls)
        for index, value in enumerate(("Authorization: Bearer " + key, key, "prefix-" + key)):
            owner = {**OWNER, "bot_id": value}
            config = Path(self.temp.name) / ("bad-owner-" + str(index) + ".json")
            save(config, {**owner, "url": "https://example.invalid/hook", "key": key})
            run = self.project / ".artifacts" / "runs" / ("refused-" + str(index))
            with self.assertRaises(BridgeError) as error:
                manager_module.Manager.create(
                    run,
                    self.project,
                    owner,
                    webhook_config=config,
                    source_home=self.source,
                    runner=self.hc,
                )
            self.assertNotIn(key, str(error.exception))
            self.assertFalse(run.exists())
        self.assertEqual(len(self.hc.calls), before)

    def test_reopening_legacy_raw_key_owner_fails_before_output_or_hc(self):
        key = "PRIVATE-BEARER"
        config = self.config()
        self.manager.configure(config)
        original = json.loads(self.manager.path.read_text())
        owner = {**OWNER, "bot_id": key}
        data = copy.deepcopy(original)
        data["owner"] = owner
        save(self.manager.path, data)
        save(config, {**owner, "url": "https://example.invalid/routine", "key": key})
        before = len(self.hc.calls)
        try:
            with self.assertRaises(BridgeError) as error:
                manager_module.Manager(self.root, owner, runner=self.hc)
            self.assertNotIn(key, str(error.exception))
            self.assertEqual(len(self.hc.calls), before)
        finally:
            save(self.manager.path, original)
            self.config()

    def test_unavailable_config_can_be_replaced_and_identity_checked_cleanup_survives(self):
        config = self.config()
        self.manager.configure(config)
        config.unlink()
        before = len(self.hc.calls)
        with self.assertRaises(BridgeError):
            manager_module.Manager(self.root, OWNER, runner=self.hc)
        self.assertEqual(len(self.hc.calls), before)
        # Core recovery remains local and identity-checked; it never returns owner fields.
        cleanup = self.manager.bridge().close()
        self.assertTrue(all(item["passed"] for item in cleanup))
        replacement = Path(self.temp.name) / "replacement.json"
        save(
            replacement,
            {**OWNER, "url": "https://example.invalid/routine", "key": "replacement-secret"},
        )
        manager = manager_module.Manager(
            self.root, OWNER, runner=self.hc, allow_config_replacement=True
        )
        with self.assertRaises(BridgeError):
            manager.envelope()
        self.assertTrue(manager.configure(replacement)["configured"])
        self.assertTrue(manager.close()["closed"])

    def test_replacement_cannot_turn_a_legacy_old_key_into_a_public_identifier(self):
        old_key, new_key = "old-webhook-private-token", "new-webhook-private-token"
        config = self.config()
        self.manager.configure(config)
        original = json.loads(self.manager.path.read_text())
        legacy_owner = {**OWNER, "bot_id": old_key}
        legacy = {**original, "owner": legacy_owner}
        replacement = Path(self.temp.name) / "new-config.json"
        save(
            replacement, {**legacy_owner, "url": "https://example.invalid/routine", "key": new_key}
        )
        before = len(self.hc.calls)
        try:
            for config_owner in (legacy_owner, OWNER):
                save(self.manager.path, legacy)
                save(
                    config,
                    {**config_owner, "url": "https://example.invalid/routine", "key": old_key},
                )
                with self.assertRaises(BridgeError) as error:
                    manager_module.Manager(
                        self.root, legacy_owner, runner=self.hc, allow_config_replacement=True
                    )
                self.assertNotIn(old_key, str(error.exception))
                self.assertNotIn(new_key, str(error.exception))
                self.assertEqual(json.loads(self.manager.path.read_text()), legacy)
            self.assertEqual(len(self.hc.calls), before)
        finally:
            save(self.manager.path, original)
            self.config()

    def test_legacy_notification_cannot_return_known_key_on_accepted_shortcut(self):
        self.manager.configure(self.config())
        self.manager.submit("analysis-1", "Read source")
        state = self.finish("analysis-1", {"summary": "fixture"})
        box = self.manager.outbox()
        data = json.loads(box.path.read_text())
        event = data["events"][state["event_id"]]
        event.update(state="accepted")
        event["payload"]["state"] = {"private": "PRIVATE-BEARER"}
        save(box.path, data)  # A synthetic legacy record, not an allowed enqueue.
        sent = []
        self.manager.sender = lambda *args: sent.append(args) or 200
        for action in (
            lambda: self.manager.notify(state["event_id"]),
            lambda: self.manager.acknowledge(state["event_id"], "receipt"),
        ):
            with self.assertRaises(BridgeError) as error:
                action()
            self.assertNotIn("PRIVATE-BEARER", str(error.exception))
        self.assertEqual(sent, [])
        self.assertEqual(json.loads(box.path.read_text()), data)

    def test_start_cannot_be_replayed_and_run_intent_precedes_hc(self):
        self.assertEqual(self.hc.count("run"), 1)
        with self.assertRaises(FileExistsError):
            manager_module.Manager.create(
                self.root, self.project, OWNER, source_home=self.source, runner=self.hc
            )
        self.assertEqual(self.hc.count("run"), 1)
        other_root = self.project / ".artifacts" / "runs" / "second"

        def inspect(args, env, timeout):
            intent = json.loads((other_root / "manager.json").read_text())
            self.assertEqual(intent["owner"], OWNER)
            self.assertEqual(intent["setup"], "creation_unknown")
            self.assertFalse(intent["closed"])
            return {"address": "own-terminal"}

        other_hc = FakeHC()
        other_hc.handlers["run"] = inspect
        other = manager_module.Manager.create(
            other_root, self.project, OWNER, source_home=self.source, runner=other_hc
        )
        other.close()

    def test_create_serializes_launch_against_close(self):
        other_root = self.project / ".artifacts" / "runs" / "concurrent"
        other_hc = FakeHC()
        launched, release, close_finished = threading.Event(), threading.Event(), threading.Event()
        created, errors = [], []

        def run(args, env, timeout):
            launched.set()
            if not release.wait(3):
                raise RuntimeError("test release missing")
            return {"address": "own-terminal"}

        other_hc.handlers["run"] = run

        def start():
            try:
                created.append(
                    manager_module.Manager.create(
                        other_root, self.project, OWNER, source_home=self.source, runner=other_hc
                    )
                )
            except Exception as exc:
                errors.append(exc)

        starter = threading.Thread(target=start)
        starter.start()
        self.assertTrue(launched.wait(3))

        def close():
            try:
                manager_module.Manager(other_root, OWNER, runner=other_hc).close()
                close_finished.set()
            except Exception as exc:
                errors.append(exc)

        closer = threading.Thread(target=close)
        closer.start()
        try:
            self.assertFalse(close_finished.wait(0.1))
        finally:
            release.set()
            starter.join(3)
            closer.join(3)
        self.assertFalse(starter.is_alive() or closer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(close_finished.is_set())
        self.assertTrue(created[0].status()["closed"])

    def test_unknown_submission_blocks_replay_and_new_task(self):
        def lost(args, env, timeout):
            raise subprocess.TimeoutExpired("fake hc", timeout)

        self.hc.handlers["send"] = lost
        result = self.manager.submit("analysis-1", "PRIVATE SUBMITTED PROMPT")
        self.assertEqual(result["task"]["delivery"], "unknown")
        self.assertFalse(result["task"]["verified_success"])
        for task in ("analysis-1", "replacement"):
            with self.subTest(task=task), self.assertRaises(BridgeError):
                self.manager.submit(task, "Must not replay")
        self.assertEqual(self.hc.count("send"), 1)

    def test_public_output_omits_native_logs_prompt_auth_and_machine_paths(self):
        self.manager.submit("analysis-1", "PRIVATE SUBMITTED PROMPT")
        result = self.finish("analysis-1", {"summary": "small source conclusion"})
        encoded = json.dumps(result)
        for private in (
            "PRIVATE SUBMITTED PROMPT",
            "PRIVATE NATIVE TRANSCRIPT",
            str(self.project),
            "submitted_text",
            "scratch",
            "config.toml",
        ):
            self.assertNotIn(private, encoded)
        self.assertEqual(
            result["task"]["evidence"]["result"]["answer"]["summary"], "small source conclusion"
        )

    def test_outcome_event_is_deduplicated_and_contains_only_pointers(self):
        self.manager.submit("analysis-1", "Read source")
        first = self.finish("analysis-1", {"summary": "conclusion"})
        second = self.manager.observe("analysis-1")
        self.assertEqual(first["event_id"], second["event_id"])
        event = self.manager.outbox().get(first["event_id"])
        self.assertEqual(event["state"], "pending")
        self.assertEqual(
            set(event["payload"]), {"event_id", "run_id", "run", "owner", "task_id", "kind"}
        )
        self.assertEqual(event["payload"]["task_id"], "analysis-1")
        self.assertEqual(len(second["event_ids"]), 1)

    def test_observing_old_task_never_returns_new_tasks_notification_id(self):
        self.manager.submit("analysis-1", "Read source")
        self.finish("analysis-1", {"summary": "first"})
        self.manager.submit("followup-1", "Follow up", parent="analysis-1")
        latest = self.finish("followup-1", {"summary": "second"})
        old = self.manager.observe("analysis-1")
        self.assertEqual(old["task"]["task_id"], "analysis-1")
        self.assertIsNone(old["event_id"])
        self.assertIsNotNone(latest["event_id"])

    def test_task_id_length_is_rejected_before_send(self):
        with self.assertRaises(BridgeError):
            self.manager.submit("a" * 257, "Read source")
        self.assertEqual(self.hc.count("send"), 0)

    def test_startup_idle_alone_cannot_trigger_send_or_ready_notification(self):
        self.hc.terminal["created_ms"] = int(time.time() * 1000)
        result = self.manager.observe()
        self.assertFalse(result["input_ready"])
        self.assertIsNone(result["event_id"])
        with self.assertRaises(manager_module.InputNotReady):
            self.manager.submit("analysis-1", "No early input")
        self.assertEqual(self.hc.count("send"), 0)
        self.assertEqual(self.manager.bridge().data["tasks"], [])
        self.hc.terminal["created_ms"] -= 11_000
        self.assertTrue(self.manager.observe()["input_ready"])
        self.manager.submit("analysis-1", "Input after the guard")
        self.assertEqual(self.hc.count("send"), 1)

    def test_startup_wait_refreshes_state_and_still_refuses_a_permission_dialog(self):
        self.hc.terminal["created_ms"] = int(time.time() * 1000)

        def blocked_after_guard(seconds):
            self.assertLessEqual(seconds, 10)
            self.hc.terminal["created_ms"] -= 11_000
            self.hc.terminal["status"]["state"] = "blocked"

        with patch.object(manager_module.time, "sleep", blocked_after_guard):
            state = self.manager.observe(wait=20)
        self.assertFalse(state["input_ready"])
        self.assertEqual(
            self.manager.outbox().get(state["event_id"])["payload"]["kind"], "permission_required"
        )
        with self.assertRaises(BridgeError):
            self.manager.submit("analysis-1", "No input into dialog")
        self.assertEqual(self.hc.count("send"), 0)

    def test_native_permission_block_has_run_association_without_fabricated_task(self):
        self.hc.terminal["status"]["state"] = "blocked"
        result = self.manager.observe()
        self.assertIsNone(result["task"])
        event = self.manager.outbox().get(result["event_id"])
        self.assertIsNone(event["payload"]["task_id"])
        self.assertEqual(event["payload"]["kind"], "permission_required")
        self.assertEqual(self.hc.count("send"), 0)

    def test_ready_run_can_wake_bound_bot_without_fabricating_a_task(self):
        self.manager.configure(self.config())
        result = self.manager.observe()
        self.assertIsNone(result["task"])
        self.assertEqual(
            self.manager.outbox().get(result["event_id"])["payload"]["kind"], "run_ready"
        )
        sent = []
        self.manager.sender = lambda url, key, body: sent.append(json.loads(body)) or 200
        self.assertEqual(self.manager.notify(result["event_id"])["state"], "accepted")
        self.assertIsNone(sent[0]["task_id"])
        self.assertEqual(sent[0]["owner"], OWNER)
        self.assertEqual(self.hc.count("send"), 0)

    def test_taskless_startup_failure_is_not_silently_watched_to_deadline(self):
        for status in ("error", "finished"):
            with self.subTest(status=status):
                self.hc.terminal["state"] = "finished" if status == "finished" else "live"
                self.hc.terminal["status"]["state"] = "error" if status == "error" else "idle"
                result = self.manager.watch(1)
                self.assertEqual(result["watch_status"], "outcome")
                event = self.manager.outbox().get(result["event_id"])
                self.assertIsNone(event["payload"]["task_id"])
                self.assertEqual(
                    event["payload"]["kind"], "agent_error" if status == "error" else "exited"
                )

    def test_http_acceptance_and_acknowledgment_do_not_verify_task(self):
        self.manager.configure(self.config())
        self.manager.submit("analysis-1", "Read source")
        result = self.finish("analysis-1", {"summary": "conclusion"})

        def sender(url, key, body):
            self.assertEqual(key, "PRIVATE-BEARER")
            self.assertNotIn("PRIVATE-BEARER", body.decode())
            self.assertEqual(json.loads(body)["owner"], OWNER)
            return 200

        self.manager.sender = sender
        accepted = self.manager.notify(result["event_id"])
        self.assertEqual(accepted["state"], "accepted")
        self.assertIsNone(accepted["acknowledgment"])
        self.manager.acknowledge(result["event_id"], "Grok observed event " + result["event_id"])
        self.assertFalse(self.manager.status("analysis-1")["task"]["verified_success"])
        self.assertNotIn("PRIVATE-BEARER", json.dumps(accepted))

    def test_verifier_uses_source_checks_and_returns_fresh_verified_state(self):
        self.manager.submit("analysis-1", manager_module.PROMPTS["analysis"])
        self.finish("analysis-1", self.analysis_answer())
        result = self.manager.verify("analysis-1", "analysis")
        self.assertTrue(result["passed"], result["checks"])
        self.assertTrue(result["task"]["verified_success"])
        self.assertTrue(self.manager.status("analysis-1")["task"]["verified_success"])

    def test_bad_result_cannot_be_promoted_and_question_is_separate(self):
        self.manager.submit("analysis-1", manager_module.PROMPTS["analysis"])
        answer = self.analysis_answer()
        answer["initial_task_ids"] = ["wrong"]
        self.finish("analysis-1", answer)
        result = self.manager.verify("analysis-1", "analysis")
        self.assertFalse(result["passed"])
        self.assertFalse(result["task"]["verified_success"])
        self.manager.submit("question-1", manager_module.PROMPTS["question"], parent="analysis-1")
        question = self.finish(
            "question-1",
            {"question": "Which language should I use for the final explanation?"},
            "question",
        )
        self.assertEqual(question["task"]["observed"], "needs_human")
        checked = self.manager.verify("question-1", "question")
        self.assertTrue(checked["passed"], checked["checks"])
        self.assertFalse(checked["task"]["verified_success"])

    def test_lost_monitor_launch_receipt_is_not_replayed(self):
        def lost(command, **kwargs):
            persisted = json.loads(self.manager.path.read_text())
            self.assertEqual(persisted["monitor"]["state"], "launch_unknown")
            self.assertIsNone(persisted["monitor"]["pid"])
            raise OSError("lost spawn reply")

        with self.assertRaises(OSError):
            self.manager.monitor(5, launcher=lost)
        with self.assertRaises(BridgeError):
            self.manager.monitor(5, launcher=lost)

    def test_close_before_monitor_registration_still_finalizes_owned_monitor(self):
        self.manager.monitor(5, launcher=lambda *a, **k: SimpleNamespace(pid=12345))
        token = json.loads(self.manager.path.read_text())["monitor"]["token"]
        self.manager.close()
        result = self.manager.watch(5, token)
        self.assertEqual(result["watch_status"], "closed")
        state = json.loads(self.manager.path.read_text())
        self.assertEqual(state["monitor"]["state"], "finished")
        self.assertEqual(state["monitor"]["reason"], "closed")

    def test_monitor_retires_before_callback_can_submit_followup(self):
        self.manager.submit("analysis-1", "Read source")
        self.finish("analysis-1", {"summary": "first"})
        self.manager.monitor(5, launcher=lambda *a, **k: SimpleNamespace(pid=12345))
        token = json.loads(self.manager.path.read_text())["monitor"]["token"]

        def callback(event_id, retry=False):
            state = json.loads(self.manager.path.read_text())
            self.assertEqual(state["monitor"]["state"], "finished")
            self.manager.submit("followup-1", "Follow up", "analysis-1")
            self.manager.monitor(5, launcher=lambda *a, **k: SimpleNamespace(pid=23456))
            return self.manager.outbox().get(event_id)

        with patch.object(self.manager, "notify", callback):
            self.assertEqual(self.manager.watch(1, token)["watch_status"], "outcome")
        state = json.loads(self.manager.path.read_text())
        self.assertNotEqual(state["monitor"]["token"], token)
        self.assertEqual(state["monitor"]["state"], "launch_unknown")
        self.assertEqual(self.hc.count("send"), 2)

    def test_background_monitor_bootstraps_and_exits_with_fake_hc_executable(self):
        self.manager.submit("analysis-1", "Read source")
        self.finish("analysis-1", {"summary": "completed fixture"})
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        executable = fake_bin / "hc"
        executable.write_text(
            "#!" + sys.executable + "\nimport json,sys\n"
            "command=sys.argv[1]\n"
            'if command == "get": print(' + repr(json.dumps(self.hc.terminal)) + ")\n"
            'elif command == "wait": print(json.dumps({"reason":"timeout"}))\n'
            'elif command == "read": print(json.dumps({"text":"fixture screen"}))\n'
            "else: raise SystemExit(99)\n"
        )
        executable.chmod(0o700)
        children = []

        def launch(command, **kwargs):
            env = {
                **kwargs.pop("env", os.environ),
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            }
            child = subprocess.Popen(command, env=env, **kwargs)
            children.append(child)
            return child

        record = self.manager.monitor(5, launcher=launch)
        try:
            self.assertEqual(children[0].wait(timeout=10), 0)
        finally:
            if children[0].poll() is None:
                children[0].terminate()
                children[0].wait(timeout=5)
        state = json.loads(self.manager.path.read_text())
        self.assertEqual(record["monitor"]["pid"], children[0].pid)
        self.assertEqual(state["monitor"]["state"], "finished")
        self.assertEqual(state["monitor"]["reason"], "outcome")

    def test_cli_preserves_submission_receipt_when_observer_launch_fails(self):
        command = [
            "grok_bridge.py",
            "--run",
            str(self.root),
            "--bot-id",
            OWNER["bot_id"],
            "--conversation-id",
            OWNER["conversation_id"],
            "submit",
            "analysis-1",
            "--contract",
            "analysis",
            "--monitor",
        ]
        output = io.StringIO()
        with (
            patch.object(sys, "argv", command),
            patch.object(sys, "stdout", output),
            patch.object(manager_module, "Manager", return_value=self.manager),
            patch.object(self.manager, "monitor", side_effect=OSError("lost process receipt")),
        ):
            code = manager_module.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["task"]["task_id"], "analysis-1")
        self.assertEqual(result["task"]["delivery"], "submitted")
        self.assertIn("monitor_error", result)
        self.assertEqual(self.hc.count("send"), 1)

    def test_foreground_watcher_notifies_one_outcome_without_resubmission(self):
        self.manager.configure(self.config())
        self.manager.submit("analysis-1", "Read source")
        result = self.finish("analysis-1", {"summary": "conclusion"})
        sent = []
        self.manager.sender = lambda url, key, body: sent.append(json.loads(body)) or 200
        watched = self.manager.watch(1)
        self.assertEqual(watched["watch_status"], "outcome")
        self.assertEqual(watched["notification"]["state"], "accepted")
        self.assertEqual(sent[0]["event_id"], result["event_id"])
        self.assertEqual(self.hc.count("send"), 1)

    def test_repeated_watch_does_not_repost_accepted_or_uncertain_notification(self):
        self.manager.configure(self.config())
        sent = []

        def send(url, key, body):
            sent.append(json.loads(body))
            if len(sent) == 2:
                raise TimeoutError("lost response, not an authorization to replay")
            return 200

        self.manager.sender = send
        parent = None
        for task_id, state in (("first", "accepted"), ("second", "unknown")):
            self.manager.submit(task_id, "Read source", parent)
            self.finish(task_id, {"summary": "conclusion"})
            first = self.manager.watch(1)
            second = self.manager.watch(1)
            self.assertEqual(first["notification"]["state"], state)
            self.assertEqual(second["notification"]["state"], state)
            parent = task_id
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.hc.count("send"), 2)

    def test_four_turn_contract_loop_keeps_one_thread_and_cleans_resources(self):
        self.manager.submit("analysis", manager_module.PROMPTS["analysis"])
        self.finish("analysis", self.analysis_answer())
        self.assertTrue(self.manager.verify("analysis", "analysis")["passed"])
        self.manager.submit("followup", manager_module.PROMPTS["followup"], "analysis")
        self.finish(
            "followup",
            {
                "method_name": "test_optimized_run_rejects_wrong_task_evidence",
                "asserted_message": "missing task evidence",
                "previous_task_id": "analysis",
                "sources": self.analysis_answer()["sources"],
            },
        )
        self.assertTrue(self.manager.verify("followup", "followup")["passed"])
        self.manager.submit("question", manager_module.PROMPTS["question"], "followup")
        self.finish(
            "question",
            {"question": "Which language should I use for the final explanation?"},
            "question",
        )
        question = self.manager.verify("question", "question")
        self.assertTrue(question["passed"])
        self.assertFalse(question["task"]["verified_success"])
        self.manager.submit("clarification", manager_module.PROMPTS["clarification"], "question")
        self.finish(
            "clarification",
            {
                "language": "English",
                "previous_question_task_id": "question",
                "done_verifies_success": False,
                "explanation": "Done is terminal state, so check the actual task evidence.",
            },
        )
        clarified = self.manager.verify("clarification", "clarification")
        self.assertTrue(clarified["passed"], clarified["checks"])
        self.assertTrue(clarified["task"]["verified_success"])
        self.assertEqual(self.hc.count("send"), 4)
        scratch = Path(self.manager.bridge().data["scratch"])
        self.assertTrue(self.manager.close()["closed"])
        self.assertFalse(scratch.exists())

    def test_cli_status_keeps_run_and_owner_guards_without_hc_calls(self):
        command = [
            sys.executable,
            str(SCRIPTS / "grok_bridge.py"),
            "--run",
            str(self.root),
            "--bot-id",
            OWNER["bot_id"],
            "--conversation-id",
            OWNER["conversation_id"],
        ]
        ok = subprocess.run([*command, "status"], text=True, capture_output=True, timeout=10)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["owner"], OWNER)
        wrong = subprocess.run(
            [*command, "--expect-run-id", "wrong", "status"],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("run identity mismatch", json.loads(wrong.stderr)["detail"])

    def test_unknown_event_and_wrong_run_are_refused(self):
        with self.assertRaises(BridgeError):
            self.manager.notify("not-this-run")
        with self.assertRaises(BridgeError):
            self.manager.acknowledge("not-this-run", "receipt")
        self.assertEqual(self.hc.count("send"), 0)


if __name__ == "__main__":
    unittest.main()
