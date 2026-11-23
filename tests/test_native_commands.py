"""Native operations through an owner-bound manager and disposable hc fixtures."""

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_grok_bridge import OWNER, FakeHC
from test_pi_evidence import SELECTION

from herdr_agent_bridge import codex_native
from herdr_agent_bridge.bridge_common import BridgeError, save
from herdr_agent_bridge.grok_bridge import Manager


class NativeDeliveryContract:
    def test_unresolved_operation_retry_keeps_ordinary_input_closed(self):
        command, arguments = self.NATIVE_COMMAND

        def lost_reply(_args, _env, timeout):
            raise subprocess.TimeoutExpired("fake hc send", timeout)

        self.hc.handlers["send"] = lost_reply
        initial = self.manager.native_command("same-operation", command, arguments)
        repeated = self.manager.native_command("same-operation", command, arguments)
        self.assertEqual(initial["native_operation"], repeated["native_operation"])
        self.assertEqual(repeated["native_operation"]["state"], "delivery_unknown")
        with self.assertRaises(BridgeError) as exc:
            self.manager.submit("ordinary-round", "Check the report.")
        self.assertEqual(exc.exception.code, "native_control_pending")
        self.assertEqual(self.hc.count("send"), 1)


class NativeCommandTests(NativeDeliveryContract, unittest.TestCase):
    NATIVE_COMMAND = ("/name", "QA report")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.hc = FakeHC()
        self.manager = Manager.create(
            self.project / ".artifacts" / "runs" / "native",
            self.project,
            OWNER,
            model=SELECTION["model"],
            thinking=SELECTION["thinking"],
            agent="pi",
            runtime="native",
            runner=self.hc,
        )
        bridge = self.manager.bridge()
        self.hc.terminal["labels"] = {"bridge_run": bridge.data["session"]}
        save(bridge.root / "pi-selection.json", {"state": "ready", **SELECTION})
        self.screen = "Pi command input"
        self.hc.handlers["read"] = lambda *_: {
            "text": self.screen,
            "complete": True,
            "how": "screen",
        }

    def test_pi_session_name_confirms_native_effect_and_retry_sends_once(self):
        def send(args, _env, _timeout):
            journal = json.loads(self.manager.bridge().path.read_text())
            record = journal["native_operations"]["rename-one"]
            self.assertEqual(record["delivery"], "unknown")
            self.assertEqual(record["arguments"], "QA report")
            self.assertEqual(args[-2:], ["own-terminal", "/name QA report"])
            self.screen += "\nSession name set: QA report"
            return {"started": False}

        self.hc.handlers["send"] = send
        first = self.manager.native_command("rename-one", "/name", "QA report")
        repeated = Manager(self.manager.root, OWNER, runner=self.hc).native_command(
            "rename-one", "/name", "QA report"
        )
        self.assertEqual(first["native_operation"]["state"], "confirmed")
        self.assertEqual(repeated["native_operation"], first["native_operation"])
        self.assertEqual(first["native_operation"]["session_id"], SELECTION["session_id"])
        self.assertEqual(self.hc.count("send"), 1)
        self.assertIsNone(first["task"])

    def test_unconfirmed_delivery_is_reconciled_without_replay(self):
        def send(_args, _env, timeout):
            raise subprocess.TimeoutExpired("fake hc send", timeout)

        self.hc.handlers["send"] = send
        first = self.manager.native_command("with-lost-reply", "/name", "QA report")
        self.assertEqual(first["native_operation"]["state"], "delivery_unknown")
        self.assertFalse(first["input_ready"])
        competing = self.manager.native_command("competing", "/name", "Another name")
        self.assertEqual(competing["native_operation"]["reason"], "native_control_pending")
        self.assertEqual(competing["native_operation"]["delivery"], "not_attempted")

        with self.assertRaises(BridgeError) as exc:
            self.manager.native_command("with-lost-reply", "/name", "Another name")
        self.assertEqual(exc.exception.code, "native_operation_conflict")
        self.assertEqual(self.hc.count("send"), 1)
        self.screen += "\nSession name set: QA report"
        result = self.manager.native_command("with-lost-reply", "/name", "QA report")
        self.assertEqual(result["native_operation"]["state"], "confirmed")
        self.assertTrue(result["input_ready"])
        self.assertEqual(self.hc.count("send"), 1)

    def test_pi_native_name_snapshot_can_confirm_before_any_session_history(self):
        bridge = self.manager.bridge()
        snapshot = bridge.root / "pi-name.json"
        save(snapshot, {"session_id": SELECTION["session_id"], "name": None, "revision": "old"})

        def change_name(*_):
            save(snapshot, {"session_id": "wrong-thread", "name": "QA report", "revision": "first"})
            return {"started": False}

        self.hc.handlers["send"] = change_name
        pending = self.manager.native_command("native-name", "/name", "QA report")
        self.assertEqual(pending["native_operation"]["state"], "delivery_unknown")
        save(
            snapshot,
            {"session_id": SELECTION["session_id"], "name": "QA report", "revision": "second"},
        )
        result = self.manager.native_command("native-name", "/name", "QA report")
        self.assertEqual(result["native_operation"]["evidence"]["source"], "pi_session")
        self.assertEqual(self.hc.count("send"), 1)

    def test_blocked_terminal_and_wrong_owner_receive_no_input(self):
        self.hc.terminal["status"]["state"] = "blocked"
        result = self.manager.native_command("rename", "/name", "QA report")
        self.assertEqual(result["native_operation"]["reason"], "native_terminal_blocked")
        foreign = {"bot_id": OWNER["bot_id"], "conversation_id": "a different conversation"}
        with self.assertRaises(BridgeError):
            Manager(self.manager.root, foreign, runner=self.hc).native_command(
                "rename", "/name", "QA report"
            )
        self.assertEqual(self.hc.count("send"), 0)

    def test_takeover_pauses_automation_before_any_native_input(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True, capture_output=True)
        self.hc = FakeHC()
        self.manager = Manager.create(
            self.project / ".artifacts" / "runs" / "development",
            self.project,
            OWNER,
            model=SELECTION["model"],
            thinking=SELECTION["thinking"],
            development={
                "task_id": "fix-reports",
                "goal": "Fix reports",
                "allowed_paths": ["report.py"],
                "acceptance": ["Reports retain their heading."],
                "checks": [{"name": "fixture", "argv": ["true"], "timeout": 5}],
            },
            agent="pi",
            runtime="native",
            runner=self.hc,
        )
        bridge = self.manager.bridge()
        self.hc.terminal["labels"] = {"bridge_run": bridge.data["session"]}
        save(bridge.root / "pi-selection.json", {"state": "ready", **SELECTION})
        self.manager.development_control("takeover", actor="person")
        with self.assertRaisesRegex(BridgeError, "automation is paused"):
            self.manager.native_command(
                "rename", "/name", "QA report", development_task="fix-reports"
            )
        self.assertEqual(self.hc.count("send"), 0)

    def test_direct_ordinary_submit_cannot_pass_unresolved_native_command(self):
        entered = threading.Event()
        release = threading.Event()

        def delayed(*_):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test failed to release blocked hc fixture")
            return {"started": False}

        self.hc.handlers["send"] = delayed
        with ThreadPoolExecutor(max_workers=2) as pool:
            native = pool.submit(self.manager.native_command, "rename", "/name", "QA report")
            try:
                self.assertTrue(entered.wait(5))
                ordinary = pool.submit(self.manager.bridge().submit, "round", "Analyze the report.")
            finally:
                release.set()
            result = native.result(timeout=5)
            self.assertEqual(result["native_operation"]["state"], "delivery_unknown")
            with self.assertRaises(BridgeError) as exc:
                ordinary.result(timeout=5)
            self.assertEqual(exc.exception.code, "native_control_pending")
        self.assertEqual(self.hc.count("send"), 1)


class NativeCodexCommandTests(NativeDeliveryContract, unittest.TestCase):
    NATIVE_COMMAND = ("/fast", "on")
    THREAD = "01a207ca-ad65-7c20-988e-4fc81ea9f653"
    GOAL_TURN = "01a207ce-7526-7330-a056-c95702d93ef2"
    OBJECTIVE = "Check the report header."
    CREATED = 1795427300643

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.home = self.project / "codex"
        self.home.mkdir()
        self.catalog = self.home / "models_cache.json"
        self.set_capabilities([{"id": "priority", "name": "Fast"}])
        self.hc = FakeHC()
        self.hc.terminal.update(
            cwd=str(self.project),
            argv=["codex", "-c", codex_native.STATUS_LINE],
            status={"app": "codex", "state": "idle"},
        )
        self.set_tier("Standard")
        self.hc.handlers["read"] = lambda *_: {
            "text": self.screen,
            "complete": True,
            "how": "screen",
        }
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            self.manager = Manager.create(
                self.project / ".artifacts" / "runs" / "native",
                self.project,
                OWNER,
                model="gpt-6.2-sol",
                thinking="high",
                agent="codex",
                runtime="native",
                runner=self.hc,
            )
        self.hc.terminal["labels"] = {"bridge_run": self.manager.bridge().data["session"]}
        self.rollout = (
            self.home
            / "sessions"
            / "2026"
            / "11"
            / "23"
            / (f"rollout-2026-11-23T17-44-12-{self.THREAD}.jsonl")
        )
        self.rollout.parent.mkdir(parents=True)
        self.write_record("session_meta", id=self.THREAD, cwd=str(self.project))
        self.goals = self.home / "goals_1.sqlite"
        connection = sqlite3.connect(self.goals)
        try:
            connection.execute(
                """CREATE TABLE thread_goals (
                    thread_id TEXT PRIMARY KEY, goal_id TEXT, objective TEXT,
                    status TEXT, created_at_ms INTEGER
                )"""
            )
            connection.commit()
        finally:
            connection.close()

    def set_capabilities(self, tiers):
        save(
            self.catalog,
            {
                "models": [
                    {
                        "slug": "gpt-6.2-sol",
                        "display_name": "GPT-6.2-Sol",
                        "service_tiers": tiers,
                    }
                ]
            },
        )

    def set_tier(self, tier):
        label = " fast" if tier == "Fast" else ""
        self.screen = (
            f"› Ask Codex\n\n  {self.THREAD} · GPT-6.2-Sol high{label} · {tier}\n  ? for shortcuts"
        )

    def write_record(self, kind, **payload):
        with self.rollout.open("a") as stream:
            stream.write(json.dumps({"type": kind, "payload": payload}) + "\n")

    def put_goal(self, status, *, session_id=None):
        connection = sqlite3.connect(self.goals)
        try:
            connection.execute(
                "INSERT OR REPLACE INTO thread_goals VALUES (?, ?, ?, ?, ?)",
                (session_id or self.THREAD, "new-goal", self.OBJECTIVE, status, self.CREATED),
            )
            connection.commit()
        finally:
            connection.close()

    def start_goal(self, *, trigger="goal"):
        self.put_goal("active")
        self.write_record(
            "event_msg",
            type="thread_goal_updated",
            threadId=self.THREAD,
            goal={
                "threadId": self.THREAD,
                "objective": self.OBJECTIVE,
                "status": "active",
                "createdAt": self.CREATED // 1000,
            },
        )
        self.write_record(
            "event_msg",
            type="task_started",
            turn_id=self.GOAL_TURN,
            turn_attribution={"turn_id": self.GOAL_TURN, "turn_trigger": trigger},
        )

    def test_unsupported_development_runtime_cannot_reserve_a_run(self):
        root = self.project / ".artifacts" / "runs" / "unsupported"
        with self.assertRaises(BridgeError) as exc:
            Manager.create(
                root,
                self.project,
                OWNER,
                agent="codex",
                runtime="isolated",
                development={"task_id": "report"},
                runner=self.hc,
            )
        self.assertEqual(exc.exception.code, "execution_policy_invalid")
        self.assertFalse(root.exists())
        self.assertEqual(self.hc.count("run"), 1)  # Only the initial native fixture was launched.

    def test_manager_reports_bound_codex_selection_before_first_prompt(self):
        self.assertFalse(self.manager.status()["input_ready"])
        observed = self.manager.observe()
        self.assertIsNone(observed["task"])
        self.assertTrue(observed["input_ready"])
        self.assertEqual(
            observed["model_selection"]["actual"],
            {
                "model": "gpt-6.2-sol",
                "thinking": "high",
                "session_id": self.THREAD,
                "service_tier": "standard",
            },
        )
        self.assertEqual(self.manager.status()["model_selection"], observed["model_selection"])
        self.assertEqual(self.hc.count("send"), 0)

    def test_manager_reports_native_trust_prompt_without_sending_any_input(self):
        self.assert_trust_prompt(
            "Folder access\n"
            + str(self.project)
            + "\nTrust this folder?\n› 1. Trust and continue\n  2. Back to Agent Command Center",
            "codex_folder_trust_required",
        )

    def test_manager_reports_hook_review_before_hc_identifies_app(self):
        self.assert_trust_prompt(
            "Hooks need review\n  1 hook is new or changed.\n"
            "Hooks can run outside the sandbox after you trust them.\n\n"
            "› 1. Review hooks\n  2. Trust all and continue\n"
            "  3. Continue without trusting (hooks won't run)\n\n  enter confirm · esc skip",
            "codex_hook_trust_required",
        )
        self.screen += "\n› Current input"
        self.assertEqual(
            self.manager.observe()["model_selection"]["reason"], "codex_terminal_pending"
        )
        self.assertEqual(self.hc.count("send"), 0)

    def assert_trust_prompt(self, screen, reason):
        self.hc.terminal["status"] = None
        self.hc.terminal["foreground"] = {
            "name": "codex",
            "cwd": str(self.project),
            "argv": ["codex", "-c", codex_native.STATUS_LINE],
        }
        self.screen = screen
        observed = self.manager.observe()
        self.assertFalse(observed["input_ready"])
        self.assertEqual(observed["model_selection"]["reason"], reason)
        self.assertIsNotNone(observed["event_id"])
        self.assertEqual(
            self.manager.outbox().get(observed["event_id"])["payload"]["kind"],
            "permission_required",
        )
        operation = self.manager.native_command("fast", "/fast")["native_operation"]
        self.assertEqual(operation["reason"], "native_terminal_blocked")
        self.assertEqual(self.hc.count("send"), 0)

    def test_fast_after_lost_reply_reconciles_actual_state_and_refuses_stale_mode(self):
        def switch(args, _env, timeout):
            self.assertEqual(args[-2:], ["own-terminal", "/fast"])
            self.set_tier("Fast")
            raise subprocess.TimeoutExpired("fake hc send", timeout)

        self.hc.handlers["send"] = switch
        result = self.manager.native_command("enable-fast", "/fast")
        self.assertEqual(result["native_operation"]["evidence"]["service_tier"], "fast")
        self.assertEqual(result["native_operation"]["state"], "confirmed")
        repeated = self.manager.native_command("enable-fast", "/fast", "on")
        self.assertEqual(repeated["native_operation"], result["native_operation"])
        already = self.manager.native_command("request-fast-again", "/fast", "on")
        self.assertTrue(already["native_operation"]["evidence"]["already_enabled"])
        self.assertEqual(already["native_operation"]["delivery"], "not_attempted")
        self.assertEqual(self.hc.count("send"), 1)

        self.set_tier("Standard")
        stale = self.manager.native_command("enable-fast", "/fast")
        self.assertEqual(stale["native_operation"]["reason"], "native_fast_drifted")
        self.assertFalse(stale["input_ready"])
        with self.assertRaises(BridgeError) as exc:
            self.manager.submit("task-after-drift", "Analyze the report.")
        self.assertEqual(exc.exception.code, "native_fast_required")
        self.assertEqual(self.hc.count("send"), 1)

    def test_unsupported_model_receives_no_fast_toggle(self):
        self.set_capabilities([{"id": "ultrafast", "name": "Ultrafast"}])
        operation = self.manager.native_command("enable-fast", "/fast", "on")["native_operation"]
        self.assertEqual(operation["state"], "unsupported")
        self.assertEqual(self.hc.count("send"), 0)

    def test_goal_turns_and_ordinary_tasks_use_separate_receipts(self):
        self.put_goal("active", session_id="an-unrelated-thread")

        def send(args, _env, _timeout):
            self.assertEqual(args[-2:], ["own-terminal", f"/goal {self.OBJECTIVE}"])
            self.start_goal()
            return {"started": False}

        self.hc.handlers["send"] = send
        result = self.manager.native_command("goal", "/goal", self.OBJECTIVE)
        operation = result["native_operation"]
        self.assertEqual(operation["state"], "goal_active")
        self.assertEqual(operation["evidence"]["goal_turn_ids"], [self.GOAL_TURN])
        self.assertFalse(result["input_ready"])
        self.assertIsNone(result["task"])
        with self.assertRaises(BridgeError) as exc:
            self.manager.submit("ordinary", "Describe the report.")
        self.assertEqual(exc.exception.code, "native_control_pending")

        self.put_goal("complete")
        still_working = self.manager.native_command("goal", "/goal", self.OBJECTIVE)
        self.assertEqual(still_working["native_operation"]["state"], "delivery_unknown")
        self.write_record("event_msg", type="task_complete", turn_id=self.GOAL_TURN)
        result = self.manager.native_command("goal", "/goal", self.OBJECTIVE)
        self.assertTrue(result["input_ready"])
        self.assertEqual(result["native_operation"]["evidence"]["goal_status"], "complete")
        self.assertEqual(result["native_operation"]["state"], "confirmed")
        self.assertEqual(self.hc.count("send"), 1)

        del self.hc.handlers["send"]
        submitted = self.manager.submit("ordinary", "Describe /goal in the report.")
        self.assertEqual(submitted["task"]["task_id"], "ordinary")
        self.assertTrue(self.hc.calls[-1][-1].endswith("Describe /goal in the report."))
        self.assertEqual(self.hc.count("send"), 2)

    def test_goal_with_unattributed_turn_blocks_followup(self):
        def send(*_):
            self.start_goal(trigger="user")
            self.put_goal("complete")
            self.write_record("event_msg", type="task_complete", turn_id=self.GOAL_TURN)
            return {}

        self.hc.handlers["send"] = send
        operation = self.manager.native_command("goal", "/goal", self.OBJECTIVE)["native_operation"]
        self.assertEqual(operation["state"], "delivery_unknown")
        with self.assertRaises(BridgeError):
            self.manager.submit("ordinary", "Describe the report.")
        self.assertEqual(self.hc.count("send"), 1)

    def test_model_menu_requires_human_selection_and_never_sends_keys(self):
        def open_menu(*_):
            self.screen = "  Select Model and Effort\n› GPT-6.2-Sol\n  Enter to select"
            return {"started": False}

        self.hc.handlers["send"] = open_menu
        pending = self.manager.native_command("model-menu", "/model")
        self.assertEqual(pending["native_operation"]["state"], "pending_selection")
        self.assertFalse(pending["input_ready"])
        with self.assertRaises(BridgeError):
            self.manager.submit("ordinary", "Describe the report.")
        self.set_tier("Standard")  # The person closed the selector.
        result = self.manager.native_command("model-menu", "/model")
        self.assertEqual(result["native_operation"]["state"], "confirmed")
        self.assertEqual(self.hc.count("send"), 1)
