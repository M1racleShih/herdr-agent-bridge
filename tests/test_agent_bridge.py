"""Safety and correlation checks using only disposable journals and fake hc calls."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_agent_bridge import agent_bridge as bridge


class FakeHC:
    def __init__(self):
        self.calls = []
        self.terminal = {
            "address": "terminal-1",
            "state": "live",
            "turn": 0,
            "status": {"state": "idle"},
        }
        self.other_terminals = []
        self.handlers = {}

    def __call__(self, args, env, timeout):
        self.calls.append((list(args), dict(env), timeout))
        command = args[0]
        if command in self.handlers:
            return self.handlers[command](args, env, timeout)
        if command == "get":
            if self.terminal is None:
                raise bridge.HcError(
                    {
                        "args": args,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": json.dumps({"error": {"kind": "not_found"}}),
                    }
                )
            return copy.deepcopy(self.terminal)
        if command == "run":
            return {"address": self.terminal["address"]}
        if command == "send":
            return {"started": True}
        if command == "read":
            return {"text": "fake screen"}
        if command == "kill":
            self.terminal["state"] = "finished"
        if command == "rm":
            self.terminal = None
        if command == "ls":
            terminals = (
                [self.terminal] if self.terminal is not None else []
            ) + self.other_terminals
            return {"terminals": copy.deepcopy(terminals)}
        return {}

    def count(self, command):
        return sum(args[0] == command for args, _, _ in self.calls)


def meta(thread="thread-1"):
    return {"type": "session_meta", "payload": {"id": thread}}


def message(role, text, turn="turn-1", phase=None):
    payload = {
        "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
    }
    if turn is not None:
        payload["internal_chat_message_metadata_passthrough"] = {"turn_id": turn}
    if phase is not None:
        payload["phase"] = phase
    return {"type": "response_item", "payload": payload}


def complete(turn="turn-1"):
    return {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}}


def final(task_id):
    return json.dumps(
        {"task_id": task_id, "kind": "result", "answer": {"summary": "fixture answer"}}
    )


class AgentBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-bridge-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def make_bridge(self, launch="submitted"):
        base = Path(tempfile.mkdtemp(dir=self.base))
        root, scratch = base / "run", base / "scratch"
        root.mkdir()
        for name in ("config", "state", "run", "cache", "codex"):
            (scratch / name).mkdir(parents=True)
        data = {
            "format": 1,
            "session": "test-session",
            "scratch": str(scratch),
            "project": str(base),
            "terminal": "agent",
            "address": "terminal-1",
            "model": "fixture",
            "launch": launch,
            "tasks": [],
            "closed": False,
        }
        bridge.save(root / "journal.json", data)
        hc = FakeHC()
        return bridge.Bridge(root, runner=hc), hc

    def rollout(self, obj, events, filename="rollout.jsonl"):
        path = Path(obj.data["scratch"]) / "codex" / "sessions" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return path

    def start_task(self, obj, hc, task_id="task-1"):
        submitted = obj.submit(task_id, "Inspect the fixture.")
        hc.terminal["turn"] = 1
        hc.terminal["status"]["state"] = "done"
        return submitted

    def assert_no_send(self, hc):
        self.assertEqual(hc.count("send"), 0)

    def test_launch_persists_uncertain_intent_before_hc_and_is_not_replayed(self):
        obj, hc = self.make_bridge(launch="not_submitted")
        obj.data["address"] = None
        obj.persist()

        def interrupted(args, env, timeout):
            on_disk = json.loads(obj.path.read_text())
            self.assertEqual(on_disk["launch"], "delivery_unknown")
            self.assertIsNone(on_disk["address"])
            self.assertEqual(env["HC_SESSION"], "test-session")
            self.assertEqual(Path(env["CODEX_HOME"]), Path(on_disk["scratch"]) / "codex")
            raise subprocess.TimeoutExpired("fake hc run", timeout)

        hc.handlers["run"] = interrupted
        with self.assertRaises(subprocess.TimeoutExpired):
            obj.launch()
        reopened = bridge.Bridge(obj.root, runner=hc)
        self.assertEqual(reopened.data["launch"], "delivery_unknown")
        with self.assertRaises(bridge.BridgeError):
            reopened.launch()
        self.assertEqual(hc.count("run"), 1)

    def test_task_intent_is_durable_before_send(self):
        obj, hc = self.make_bridge()

        def inspect_intent(args, env, timeout):
            on_disk = json.loads(obj.path.read_text())
            self.assertEqual(len(on_disk["tasks"]), 1)
            task = on_disk["tasks"][0]
            self.assertEqual(task["id"], "first")
            self.assertEqual(task["delivery"], "unknown")
            self.assertFalse(task["verified_success"])
            self.assertEqual(task["submitted_text"], args[-1])
            # The send must use the checked address, not a reusable terminal name.
            self.assertIn("terminal-1", args[:-1])
            self.assertNotIn("agent", args[:-1])
            self.assertEqual(task["input_sha256"], bridge.digest(args[-1]))
            self.assertEqual(Path(env["XDG_STATE_HOME"]), Path(on_disk["scratch"]) / "state")
            return {"started": True}

        hc.handlers["send"] = inspect_intent
        result = obj.submit("first", "Inspect the fixture.")
        self.assertEqual(result["delivery"], "submitted")
        self.assertEqual(hc.count("send"), 1)

    def test_uncertain_send_cannot_be_replayed_or_followed_by_new_task(self):
        for failure in ("timeout", "error_reply", "connection_loss"):
            with self.subTest(failure=failure):
                obj, hc = self.make_bridge()

                def fail(args, env, timeout):
                    if failure == "timeout":
                        raise subprocess.TimeoutExpired("fake hc send", timeout)
                    if failure == "error_reply":
                        raise bridge.HcError(
                            {"args": args, "exit_code": 1, "stdout": "", "stderr": "lost reply"}
                        )
                    raise OSError("connection lost")

                hc.handlers["send"] = fail
                result = obj.submit("first", "Inspect the fixture.")
                self.assertEqual(result["delivery"], "unknown")
                self.assertTrue(result.get("delivery_error"))
                # Reopen the persisted state: process-local guards are insufficient.
                reopened = bridge.Bridge(obj.root, runner=hc)
                with self.assertRaises(bridge.BridgeError):
                    reopened.submit("first", "Inspect the fixture.")
                with self.assertRaises(bridge.BridgeError):
                    reopened.submit("next", "Follow up.", parent="first")
                self.assertEqual(hc.count("send"), 1)
                self.assertEqual(
                    [t["id"] for t in json.loads(obj.path.read_text())["tasks"]], ["first"]
                )

    def test_non_ready_terminal_never_receives_ordinary_input(self):
        cases = [("live", "blocked"), ("live", "error"), ("live", "finished"), ("exited", "done")]
        for terminal_state, status in cases:
            with self.subTest(terminal_state=terminal_state, status=status):
                obj, hc = self.make_bridge()
                hc.terminal["state"] = terminal_state
                hc.terminal["status"]["state"] = status
                with self.assertRaises(bridge.BridgeError):
                    obj.submit("first", "Do not send this.")
                self.assert_no_send(hc)
                self.assertEqual(json.loads(obj.path.read_text())["tasks"], [])

    def test_follow_up_names_latest_resolved_task(self):
        obj, hc = self.make_bridge()
        first = self.start_task(obj, hc, task_id="first")
        events = [
            meta(),
            message("user", first["submitted_text"], turn="turn-1"),
            message("assistant", final("first"), turn="turn-1", phase="final_answer"),
            complete("turn-1"),
        ]
        self.rollout(obj, events)
        self.assertEqual(obj.observe("first")["observed"], "evidence_ready")
        second = obj.submit("second", "Follow up.", parent="first")
        hc.terminal["turn"] = 2
        events.extend(
            [
                message("user", second["submitted_text"], turn="turn-2"),
                message("assistant", final("second"), turn="turn-2", phase="final_answer"),
                complete("turn-2"),
            ]
        )
        self.rollout(obj, events)
        self.assertEqual(obj.observe("second")["observed"], "evidence_ready")
        for parent in (None, "first", "unrelated"):
            with self.subTest(parent=parent):
                with self.assertRaises(bridge.BridgeError):
                    obj.submit("third", "Follow up.", parent=parent)
                self.assertEqual(hc.count("send"), 2)
        result = obj.submit("third", "Follow up.", parent="second")
        self.assertEqual(result["parent"], "second")
        self.assertEqual(hc.count("send"), 3)

    def test_blocked_without_completed_evidence_does_not_resolve_previous_task(self):
        obj, hc = self.make_bridge()
        obj.submit("first", "Inspect the fixture.")
        hc.terminal["status"]["state"] = "blocked"
        self.assertEqual(obj.observe("first")["observed"], "needs_human")
        hc.terminal["status"]["state"] = "idle"
        with self.assertRaises(bridge.BridgeError):
            obj.submit("second", "Follow up.", parent="first")
        self.assertEqual(hc.count("send"), 1)

    def test_completed_question_can_be_followed_up_by_naming_its_task(self):
        obj, hc = self.make_bridge()
        task = self.start_task(obj, hc, task_id="first")
        question = json.dumps(
            {"task_id": "first", "kind": "question", "answer": {"question": "Which fixture?"}}
        )
        self.rollout(
            obj,
            [
                meta(),
                message("user", task["submitted_text"]),
                message("assistant", question, phase="final_answer"),
                complete(),
            ],
        )
        result = obj.observe("first")
        self.assertEqual(result["observed"], "needs_human")
        self.assertFalse(result["verified_success"])
        follow_up = obj.submit("second", "Use fixture A.", parent="first")
        self.assertEqual(follow_up["parent"], "first")
        self.assertEqual(hc.count("send"), 2)

    def test_terminal_identity_mismatch_refuses_observation_and_submission(self):
        for action in ("observe", "submit"):
            with self.subTest(action=action):
                obj, hc = self.make_bridge()
                hc.terminal["address"] = "a-different-terminal"
                with self.assertRaises(bridge.BridgeError):
                    if action == "observe":
                        obj.observe()
                    else:
                        obj.submit("first", "Do not send this.")
                self.assert_no_send(hc)
                persisted = json.loads(obj.path.read_text())
                self.assertEqual(persisted["address"], "terminal-1")
                self.assertEqual(persisted["tasks"], [])

    def test_cleanup_rejects_a_different_terminal_without_touching_it(self):
        obj, hc = self.make_bridge()
        hc.terminal["address"] = "a-different-terminal"
        scratch = Path(obj.data["scratch"])
        with self.assertRaises(bridge.BridgeError):
            obj.close()
        for command in ("kill", "rm", "server"):
            self.assertEqual(hc.count(command), 0)
        self.assertTrue(scratch.is_dir())
        self.assertFalse(json.loads(obj.path.read_text())["closed"])

    def test_unreceipted_launch_requires_matching_session_identity(self):
        for label in (None, "another-session"):
            with self.subTest(label=label):
                obj, hc = self.make_bridge(launch="delivery_unknown")
                obj.data["address"] = None
                obj.persist()
                if label:
                    hc.terminal["labels"] = {"bridge_run": label}
                with self.assertRaises(bridge.BridgeError):
                    obj.observe()
                self.assertIsNone(json.loads(obj.path.read_text())["address"])
                self.assert_no_send(hc)
        obj, hc = self.make_bridge(launch="delivery_unknown")
        obj.data["address"] = None
        obj.persist()
        hc.terminal["labels"] = {"bridge_run": "test-session"}
        obj.observe()
        saved = json.loads(obj.path.read_text())
        self.assertEqual(saved["address"], "terminal-1")
        self.assertEqual(saved["launch"], "observed")
        self.assertEqual(hc.count("run"), 0)

    def test_correlated_completed_answer_is_evidence_but_not_verified_success(self):
        obj, hc = self.make_bridge()
        task = self.start_task(obj, hc)
        self.rollout(
            obj,
            [
                meta(),
                message("user", task["submitted_text"]),
                message("assistant", final("task-1"), phase="final_answer"),
                complete(),
            ],
        )
        result = obj.observe("task-1")
        self.assertEqual(result["observed"], "evidence_ready")
        self.assertEqual(result["delivery"], "received")
        self.assertEqual(result["evidence"]["thread_id"], "thread-1")
        self.assertEqual(result["evidence"]["agent_turn_id"], "turn-1")
        self.assertFalse(result["verified_success"])
        self.assertFalse(json.loads(obj.path.read_text())["tasks"][0]["verified_success"])

    def test_status_done_alone_cannot_verify_success(self):
        obj, hc = self.make_bridge()
        self.start_task(obj, hc)
        result = obj.observe("task-1")
        self.assertNotEqual(result["observed"], "evidence_ready")
        self.assertFalse(result["verified_success"])
        with self.assertRaises(bridge.BridgeError):
            obj.verify("task-1", [{"check": "independent fixture check", "passed": True}])

    def test_answer_requires_exact_user_native_turn_final_and_completion(self):
        scenarios = {
            # A prompt echo is not a submitted user message.
            "prompt_echo": lambda text: [
                message("assistant", text, phase="commentary"),
                message("assistant", final("task-1"), phase="final_answer"),
                complete(),
            ],
            "inexact_user": lambda text: [
                message("user", text + " extra"),
                message("assistant", final("task-1"), phase="final_answer"),
                complete(),
            ],
            "missing_user_turn": lambda text: [
                message("user", text, turn=None),
                message("assistant", final("task-1"), phase="final_answer"),
                complete(),
            ],
            "commentary_only": lambda text: [
                message("user", text),
                message("assistant", final("task-1"), phase="commentary"),
                complete(),
            ],
            "missing_final_turn": lambda text: [
                message("user", text),
                message("assistant", final("task-1"), turn=None, phase="final_answer"),
                complete(),
            ],
            "different_final_turn": lambda text: [
                message("user", text, turn="new"),
                message("assistant", final("task-1"), turn="old", phase="final_answer"),
                complete("new"),
            ],
            "different_completion_turn": lambda text: [
                message("user", text, turn="new"),
                message("assistant", final("task-1"), turn="new", phase="final_answer"),
                complete("old"),
            ],
            "missing_completion": lambda text: [
                message("user", text),
                message("assistant", final("task-1"), phase="final_answer"),
            ],
            "old_task_answer": lambda text: [
                message("user", text),
                message("assistant", final("old-task"), phase="final_answer"),
                complete(),
            ],
            "answer_before_user": lambda text: [
                message("assistant", final("task-1"), phase="final_answer"),
                message("user", text),
                complete(),
            ],
            "completion_before_answer": lambda text: [
                message("user", text),
                complete(),
                message("assistant", final("task-1"), phase="final_answer"),
            ],
        }
        for name, events in scenarios.items():
            with self.subTest(scenario=name):
                obj, hc = self.make_bridge()
                task = self.start_task(obj, hc)
                self.rollout(obj, [meta(), *events(task["submitted_text"])])
                result = obj.observe("task-1")
                self.assertNotEqual(result["observed"], "evidence_ready")
                self.assertFalse(result["verified_success"])
                with self.assertRaises(bridge.BridgeError):
                    obj.verify("task-1", [{"check": "independent fixture check", "passed": True}])

    def test_multiple_rollout_threads_cannot_be_correlated(self):
        obj, hc = self.make_bridge()
        task = self.start_task(obj, hc)
        self.rollout(
            obj,
            [
                meta(),
                message("user", task["submitted_text"]),
                message("assistant", final("task-1"), phase="final_answer"),
                complete(),
            ],
            "one.jsonl",
        )
        self.rollout(obj, [meta("thread-2")], "two.jsonl")
        result = obj.observe("task-1")
        self.assertNotEqual(result["observed"], "evidence_ready")
        self.assertNotEqual(result["delivery"], "received")
        self.assertFalse(result["verified_success"])

    def test_duplicate_user_text_in_different_turns_is_ambiguous(self):
        obj, hc = self.make_bridge()
        task = self.start_task(obj, hc)
        text = task["submitted_text"]
        self.rollout(
            obj,
            [
                meta(),
                message("user", text, turn="old"),
                message("assistant", final("task-1"), turn="old", phase="final_answer"),
                complete("old"),
                message("user", text, turn="new"),
                message("assistant", final("task-1"), turn="new", phase="final_answer"),
                complete("new"),
            ],
        )
        result = obj.observe("task-1")
        self.assertNotEqual(result["observed"], "evidence_ready")
        self.assertFalse(result["verified_success"])

    def test_corrupt_middle_of_rollout_cannot_support_evidence(self):
        obj, hc = self.make_bridge()
        task = self.start_task(obj, hc)
        path = self.rollout(
            obj,
            [
                meta(),
                message("user", task["submitted_text"]),
                message("assistant", final("task-1"), phase="final_answer"),
                complete(),
            ],
        )
        lines = path.read_text().splitlines()
        lines.insert(2, "{not valid json}")
        path.write_text("\n".join(lines) + "\n")
        result = obj.observe("task-1")
        self.assertNotEqual(result["observed"], "evidence_ready")
        self.assertFalse(result["verified_success"])

    def test_multiple_session_identities_in_one_rollout_are_ambiguous(self):
        obj, hc = self.make_bridge()
        task = self.start_task(obj, hc)
        self.rollout(
            obj,
            [
                meta(),
                message("user", task["submitted_text"]),
                meta("thread-2"),
                message("assistant", final("task-1"), phase="final_answer"),
                complete(),
            ],
        )
        result = obj.observe("task-1")
        self.assertNotEqual(result["observed"], "evidence_ready")
        self.assertFalse(result["verified_success"])

    def test_verification_requires_explicit_passing_independent_checks(self):
        obj, hc = self.make_bridge()
        task = self.start_task(obj, hc)
        self.rollout(
            obj,
            [
                meta(),
                message("user", task["submitted_text"]),
                message("assistant", final("task-1"), phase="final_answer"),
                complete(),
            ],
        )
        obj.observe("task-1")
        bad_checks = [
            [],
            [{}],
            [{"check": "fixture", "passed": False}],
            [{"check": "fixture", "passed": 1}],
            [{"passed": True}],
            ["done"],
            [{"check": "first", "passed": True}, {"check": "second", "passed": False}],
        ]
        for checks in bad_checks:
            with self.subTest(checks=checks):
                with self.assertRaises(bridge.BridgeError):
                    obj.verify("task-1", checks)
                self.assertFalse(json.loads(obj.path.read_text())["tasks"][0]["verified_success"])
        checks = [{"check": "independently inspected fixture output", "passed": True}]
        obj.verify("task-1", checks)
        saved = json.loads(obj.path.read_text())["tasks"][0]
        self.assertTrue(saved["verified_success"])
        self.assertEqual(saved["verification"], checks)

    def test_cleanup_failure_preserves_scratch_and_open_journal(self):
        for failure_command in ("kill", "rm", "server"):
            with self.subTest(failure_command=failure_command):
                obj, hc = self.make_bridge()
                scratch = Path(obj.data["scratch"])

                def fail(args, env, timeout):
                    raise bridge.HcError(
                        {"args": args, "exit_code": 1, "stdout": "", "stderr": "cleanup failed"}
                    )

                hc.handlers[failure_command] = fail
                result = obj.close()
                self.assertTrue(any(not item["passed"] for item in result))
                self.assertTrue(scratch.is_dir())
                saved = json.loads(obj.path.read_text())
                self.assertFalse(saved["closed"])
                self.assertTrue(any(not item["passed"] for item in saved["cleanup"]))
                if failure_command == "kill":
                    self.assertEqual(hc.count("rm"), 0)
                if failure_command != "server":
                    self.assertEqual(hc.count("server"), 0)

    def test_cleanup_with_unavailable_identity_keeps_recovery_data(self):
        obj, hc = self.make_bridge()
        scratch = Path(obj.data["scratch"])

        def fail(args, env, timeout):
            raise subprocess.TimeoutExpired("fake hc get", timeout)

        hc.handlers["get"] = fail
        with self.assertRaises(subprocess.TimeoutExpired):
            obj.close()
        for command in ("kill", "rm", "server"):
            self.assertEqual(hc.count(command), 0)
        self.assertTrue(scratch.is_dir())
        self.assertFalse(json.loads(obj.path.read_text())["closed"])

    def test_timed_out_removal_is_reconciled_without_replaying_it(self):
        obj, hc = self.make_bridge()
        scratch = Path(obj.data["scratch"])

        def removed_but_reply_lost(args, env, timeout):
            hc.terminal = None
            raise subprocess.TimeoutExpired("fake hc rm", timeout)

        hc.handlers["rm"] = removed_but_reply_lost
        result = obj.close()
        self.assertTrue(all(item["passed"] for item in result))
        self.assertEqual(hc.count("rm"), 1)
        self.assertGreaterEqual(hc.count("get"), 2)
        self.assertEqual(hc.count("server"), 1)
        self.assertFalse(scratch.exists())
        self.assertTrue(json.loads(obj.path.read_text())["closed"])

    def test_close_resumes_after_removed_terminal_and_failed_server_stop(self):
        obj, hc = self.make_bridge()
        scratch = Path(obj.data["scratch"])

        def fail_once(args, env, timeout):
            if hc.count("server") == 1:
                raise bridge.HcError(
                    {"args": args, "exit_code": 1, "stdout": "", "stderr": "server stop failed"}
                )
            return {}

        hc.handlers["server"] = fail_once
        first = obj.close()
        self.assertTrue(any(not item["passed"] for item in first))
        self.assertTrue(scratch.is_dir())
        self.assertFalse(json.loads(obj.path.read_text())["closed"])
        self.assertIsNone(hc.terminal)
        reopened = bridge.Bridge(obj.root, runner=hc)
        second = reopened.close()
        self.assertTrue(all(item["passed"] for item in second))
        self.assertEqual(hc.count("kill"), 1)
        self.assertEqual(hc.count("rm"), 1)
        self.assertEqual(hc.count("server"), 2)
        self.assertFalse(scratch.exists())
        self.assertTrue(json.loads(obj.path.read_text())["closed"])

    def test_cleanup_preserves_core_when_another_live_terminal_exists(self):
        obj, hc = self.make_bridge()
        scratch = Path(obj.data["scratch"])
        hc.other_terminals = [{"address": "unexpected-live", "state": "live"}]
        result = obj.close()
        self.assertTrue(any(not item["passed"] for item in result))
        self.assertEqual(hc.count("server"), 0)
        self.assertEqual(hc.count("kill"), 1)
        self.assertEqual(hc.count("rm"), 1)
        self.assertTrue(scratch.is_dir())
        self.assertFalse(json.loads(obj.path.read_text())["closed"])

    def test_cleanup_does_not_treat_an_unreadable_terminal_listing_as_empty(self):
        obj, hc = self.make_bridge()
        scratch = Path(obj.data["scratch"])
        hc.handlers["ls"] = lambda args, env, timeout: {}
        try:
            result = obj.close()
        except bridge.BridgeError:
            pass
        else:
            self.assertTrue(any(not item["passed"] for item in result))
        self.assertEqual(hc.count("server"), 0)
        self.assertTrue(scratch.is_dir())
        self.assertFalse(json.loads(obj.path.read_text())["closed"])

    def test_repeated_close_of_closed_journal_makes_no_hc_calls(self):
        obj, hc = self.make_bridge()
        first = obj.close()
        self.assertTrue(all(item["passed"] for item in first))
        calls_after_close = list(hc.calls)
        reopened = bridge.Bridge(obj.root, runner=hc)
        second = reopened.close()
        self.assertEqual(hc.calls, calls_after_close)
        self.assertEqual(second, json.loads(obj.path.read_text())["cleanup"])

    def test_untyped_not_found_error_cannot_authorize_cleanup(self):
        obj, hc = self.make_bridge()
        scratch = Path(obj.data["scratch"])

        def ambiguous_error(args, env, timeout):
            raise bridge.HcError(
                {"args": args, "exit_code": 1, "stdout": "", "stderr": "not found"}
            )

        hc.handlers["get"] = ambiguous_error
        with self.assertRaises(bridge.HcError):
            obj.close()
        for command in ("kill", "rm", "server"):
            self.assertEqual(hc.count(command), 0)
        self.assertTrue(scratch.is_dir())
        self.assertFalse(json.loads(obj.path.read_text())["closed"])

    def test_successful_cleanup_closes_journal_and_removes_scratch(self):
        obj, hc = self.make_bridge()
        scratch = Path(obj.data["scratch"])
        result = obj.close()
        self.assertTrue(result)
        self.assertTrue(all(item["passed"] for item in result))
        for command in ("kill", "rm"):
            calls = [args for args, _, _ in hc.calls if args[0] == command]
            self.assertEqual(calls, [[command, "terminal-1"]])
        self.assertFalse(scratch.exists())
        self.assertTrue(json.loads(obj.path.read_text())["closed"])


if __name__ == "__main__":
    unittest.main()
