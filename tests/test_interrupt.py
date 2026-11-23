"""Interrupt contracts with disposable Pi journals and fake hc transport."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_pi_bridge import FakeHC
from test_pi_evidence import SELECTION, assistant, selected_prefix, user

from herdr_agent_bridge.agent_bridge import Bridge, BridgeError, save
from herdr_agent_bridge.project_bridge import Project


class InterruptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.hc = FakeHC()
        self.bridge = Bridge.create(
            self.base / "run",
            self.base,
            agent="pi",
            runtime="native",
            model=SELECTION["model"],
            thinking="high",
            runner=self.hc,
        )
        self.hc.terminal["labels"] = {"bridge_run": self.bridge.data["session"]}
        self.bridge.launch()
        save(self.bridge.root / "pi-selection.json", {"state": "ready", **SELECTION})
        self.path = Path(self.bridge.data["scratch"]) / "pi" / "sessions" / "session.jsonl"
        self.entries = selected_prefix()
        self.bridge.submit("first", "Investigate the code.")
        self.entries.append(user("u", "level", self.bridge.data["tasks"][0]["submitted_text"]))
        self.write()
        self.hc.terminal["status"]["state"] = "working"

    def write(self):
        self.path.write_text("".join(json.dumps(entry) + "\n" for entry in self.entries))

    def stopped(self, args, env, timeout):
        self.entries.append(assistant("a", "u", [], stop="aborted"))
        self.write()
        self.hc.terminal["status"]["state"] = "done"
        self.hc.terminal["turn"] += 1
        save(
            self.bridge.root / "pi-settled.json",
            {
                "state": "settled",
                "session_id": SELECTION["session_id"],
                "final_message_id": None,
                "aborted_message_id": "a",
                "aborted": True,
            },
        )
        return {"outcome": "stopped"}

    def test_interrupt_preserves_session_and_allows_a_followup_to_the_aborted_round(self):
        self.hc.handlers["interrupt"] = self.stopped
        result = self.bridge.interrupt("first", actor="operator", note="Change direction")
        self.assertEqual(result["state"], "stopped")
        first = self.bridge.observe("first")
        self.assertEqual(first["observed"], "interrupted")
        self.assertFalse(first["verified_success"])
        self.assertFalse(first["evidence"]["agent_completed"])
        followup = self.bridge.submit("second", "Focus on the parser.", parent="first")
        self.assertEqual(followup["parent"], "first")
        self.assertEqual(self.hc.count("run"), 1)
        self.assertEqual(self.hc.count("kill"), 0)
        self.assertEqual(self.hc.count("interrupt"), 1)

    def test_project_entry_exposes_interrupt_with_the_original_request_and_round(self):
        config = self.base / "project.json"
        Project.initialize(
            config,
            "fixture",
            self.base,
            {"bot_id": "fixture-bot", "conversation_id": "fixture-conversation"},
        )
        entry = Project(config, runner=self.hc)
        entry.start("request", model=SELECTION["model"], thinking="high")
        self.bridge = entry.manager(entry.record()[1]).bridge()
        self.hc.terminal["labels"] = {"bridge_run": self.bridge.data["session"]}
        self.hc.terminal["status"]["state"] = "idle"
        save(self.bridge.root / "pi-selection.json", {"state": "ready", **SELECTION})
        entry.submit("first", "Investigate the code.")
        self.path = Path(self.bridge.data["scratch"]) / "pi" / "sessions" / "session.jsonl"
        self.bridge = entry.manager(entry.record()[1]).bridge()
        self.entries = selected_prefix() + [
            user("u", "level", self.bridge.data["tasks"][0]["submitted_text"])
        ]
        self.write()
        self.hc.terminal["status"]["state"] = "working"
        self.hc.handlers["interrupt"] = self.stopped
        result = entry.interrupt(
            "first", actor="operator", note="Change direction", request_id="request"
        )
        self.assertEqual(result["request_id"], "request")
        self.assertEqual(result["task"]["observed"], "interrupted")
        self.assertEqual(result["task"]["interrupt"]["state"], "stopped")
        self.assertTrue(result["input_ready"])
        self.assertEqual(
            entry.submit("second", "Focus on the parser.", parent="first")["task"]["parent"],
            "first",
        )

    def test_interrupted_round_remains_interrupted_while_the_followup_is_working(self):
        self.hc.handlers["interrupt"] = self.stopped
        self.bridge.interrupt("first", actor="operator", note="Change direction")
        self.bridge.submit("second", "Continue.", parent="first")
        self.entries.append(user("u2", "a", self.bridge.data["tasks"][-1]["submitted_text"]))
        self.write()
        self.hc.terminal["status"]["state"] = "working"
        self.assertEqual(self.bridge.observe("first")["observed"], "interrupted")

    def test_lost_stop_reply_is_reconciled_from_native_abort_without_replaying_stop(self):
        def lost(args, env, timeout):
            on_disk = json.loads(self.bridge.path.read_text())
            self.assertEqual(on_disk["tasks"][0]["interrupt"]["state"], "unknown")
            self.stopped(args, env, timeout)
            raise subprocess.TimeoutExpired("hc interrupt", timeout)

        self.hc.handlers["interrupt"] = lost
        result = self.bridge.interrupt("first", actor="operator", note="Stop")
        self.assertEqual(result["state"], "stopped")
        repeated = Bridge(self.bridge.root, runner=self.hc).interrupt(
            "first", actor="operator", note="Stop"
        )
        self.assertEqual(repeated["state"], "stopped")
        self.assertNotIn("receipt", repeated)
        self.assertEqual(self.hc.count("interrupt"), 1)

    def test_unconfirmed_stop_keeps_followup_closed_and_is_not_replayed(self):
        self.hc.handlers["interrupt"] = lambda *_: {"outcome": "timeout"}
        result = self.bridge.interrupt("first", actor="operator", note="Stop")
        self.assertEqual(result["state"], "unknown")
        with self.assertRaises(BridgeError):
            self.bridge.submit("second", "Continue", parent="first")
        self.bridge.interrupt("first", actor="operator", note="Stop")
        self.assertEqual(self.hc.count("interrupt"), 1)

    def test_wrong_terminal_identity_and_blocked_state_are_never_interrupted(self):
        self.hc.terminal["labels"]["bridge_run"] = "unrelated"
        with self.assertRaises(BridgeError):
            self.bridge.interrupt("first", actor="operator", note="Stop")
        self.hc.terminal["labels"]["bridge_run"] = self.bridge.data["session"]
        self.hc.terminal["status"]["state"] = "blocked"
        with self.assertRaises(BridgeError):
            self.bridge.interrupt("first", actor="operator", note="Stop")
        self.assertEqual(self.hc.count("interrupt"), 0)
