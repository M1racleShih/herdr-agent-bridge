"""Native recovery contracts with disposable projects, session files and fake hc."""

import copy
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_pi_bridge import FakeHC
from test_pi_evidence import SELECTION, assistant, selected_prefix, user

from herdr_agent_bridge.agent_bridge import Bridge, BridgeError, save


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.base)], check=True)
        (self.base / ".gitignore").write_text(".artifacts/\n")
        (self.base / "code.py").write_text("value = 1\n")
        self.hc = FakeHC()
        self.hc.terminal.update(pid=101, keep=True, argv=["pi"])
        self.bridge = Bridge.create(
            self.base / ".artifacts" / "run",
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
        self.bridge.submit("first", "Remember context and investigate.")
        self.path = Path(self.bridge.data["scratch"]) / "pi" / "sessions" / "session.jsonl"
        self.entries = selected_prefix() + [
            user("u", "level", self.bridge.data["tasks"][0]["submitted_text"])
        ]
        self.path.write_text("".join(json.dumps(entry) + "\n" for entry in self.entries))

    def lose(self):
        self.hc.terminal.update(pid=202, argv=["/bin/sh"], status=None)

    def resume(self, args, env, timeout):
        command = shlex.split(args[-1])
        self.assertEqual(command[0], "exec")
        self.assertEqual(command[command.index("--session") + 1], str(self.path))
        self.assertNotIn(self.bridge.data["tasks"][0]["submitted_text"], command)
        token = next(
            item.split("=", 1)[1] for item in command if item.startswith("HAB_PI_RECOVERY_TOKEN=")
        )
        save(
            self.bridge.root / "pi-selection.json",
            {"state": "ready", **SELECTION, "startup_token": token},
        )
        save(
            self.bridge.root / "pi-settled.json",
            {"state": "starting", "session_id": SELECTION["session_id"]},
        )
        self.hc.terminal.update(
            argv=["pi", "--session", str(self.path)], status={"state": "idle", "app": "pi"}
        )
        return {"started": True}

    def test_loss_closes_input_and_recovery_status_binds_code_session_and_shell(self):
        self.lose()
        status = self.bridge.recovery_status()
        self.assertEqual(status["state"], "needed")
        self.assertTrue(status["fingerprint"])
        with self.assertRaises(BridgeError):
            self.bridge.submit("second", "Continue.", parent="first")
        self.assertEqual(self.hc.count("send"), 1)

    def test_reviewed_recovery_resumes_the_exact_session_without_replaying_the_task(self):
        self.lose()
        reviewed = self.bridge.recovery_status()

        self.hc.handlers["send"] = self.resume
        result = self.bridge.recover(
            reviewed["fingerprint"], actor="operator", review="Reviewed code and session"
        )
        self.assertEqual(result["state"], "restored")
        self.assertEqual(self.hc.count("run"), 1)
        self.assertEqual(self.hc.count("send"), 2)
        first = self.bridge.observe("first")
        self.assertEqual(first["observed"], "interrupted")
        self.assertTrue(first["evidence"]["agent_lost"])
        self.assertFalse(first["verified_success"])
        self.hc.handlers.pop("send")
        self.bridge.submit("second", "Continue from the saved context.", parent="first")
        self.assertEqual(self.hc.count("send"), 3)

    def test_recovery_after_a_completed_round_keeps_its_result_available_for_followup(self):
        self.entries.append(
            assistant("a", "u", json.dumps({"task_id": "first", "kind": "result", "answer": {}}))
        )
        self.path.write_text("".join(json.dumps(entry) + "\n" for entry in self.entries))
        self.hc.terminal.update(status={"state": "done"}, turn=1)
        save(
            self.bridge.root / "pi-settled.json",
            {
                "state": "settled",
                "session_id": SELECTION["session_id"],
                "final_message_id": "a",
                "aborted": False,
            },
        )
        self.assertEqual(self.bridge.observe("first")["observed"], "evidence_ready")
        self.lose()
        reviewed = self.bridge.recovery_status()
        self.hc.handlers["send"] = self.resume
        self.bridge.recover(
            reviewed["fingerprint"], actor="operator", review="Reviewed saved result"
        )
        self.assertEqual(self.bridge.observe("first")["observed"], "evidence_ready")

    def test_existing_journal_uses_saved_process_identity_to_detect_loss(self):
        self.bridge.data.pop("agent_pid", None)
        self.bridge.data["terminal_snapshot"] = dict(self.hc.terminal)
        self.bridge.persist()
        self.lose()
        self.assertEqual(self.bridge.recovery_status()["state"], "needed")

    def test_replacement_shell_is_detected_even_if_its_pid_matches_the_old_pid(self):
        self.hc.terminal.update(argv=["/bin/sh"], status=None)
        self.assertEqual(self.bridge.recovery_status()["state"], "needed")

    def test_code_or_session_changes_require_a_new_review(self):
        self.lose()
        reviewed = self.bridge.recovery_status()
        (self.base / "code.py").write_text("value = 2\n")
        with self.assertRaisesRegex(BridgeError, "changed since review"):
            self.bridge.recover(reviewed["fingerprint"], actor="operator", review="Reviewed")
        self.assertEqual(self.hc.count("send"), 1)

    def test_unknown_resume_is_not_replayed_and_requires_a_fresh_startup_token(self):
        self.lose()
        reviewed = self.bridge.recovery_status()
        attempted = []

        def lost(args, env, timeout):
            attempted.append((args, env, timeout))
            raise subprocess.TimeoutExpired("hc send", timeout)

        self.hc.handlers["send"] = lost
        result = self.bridge.recover(reviewed["fingerprint"], actor="operator", review="Reviewed")
        self.assertEqual(result["state"], "launch_unknown")
        save(
            self.bridge.root / "pi-selection.json",
            {"state": "ready", **SELECTION, "startup_token": "stale"},
        )
        self.hc.terminal["status"] = {"state": "idle", "app": "pi"}
        self.bridge.observe()
        self.assertEqual(self.bridge.data["recovery"]["state"], "launch_unknown")
        self.bridge.recover(reviewed["fingerprint"], actor="operator", review="Reviewed")
        self.assertEqual(self.hc.count("send"), 2)
        self.resume(*attempted[0])
        self.bridge.observe()
        self.assertEqual(self.bridge.data["recovery"]["state"], "restored")
        self.assertEqual(self.hc.count("send"), 2)

    def test_recovery_refuses_an_unrelated_terminal(self):
        self.lose()
        reviewed = self.bridge.recovery_status()
        self.hc.terminal["labels"]["bridge_run"] = "unrelated"
        with self.assertRaises(BridgeError):
            self.bridge.recover(reviewed["fingerprint"], actor="operator", review="Reviewed")
        self.assertEqual(self.hc.count("send"), 1)

    def test_shell_is_rechecked_before_pasting_the_resume_command(self):
        self.lose()
        reviewed = self.bridge.recovery_status()

        def changed_after_read(args, env, timeout):
            observed = copy.deepcopy(self.hc.terminal)
            self.hc.terminal.update(pid=303, argv=["some-other-program"])
            return observed

        self.hc.handlers["get"] = changed_after_read
        with self.assertRaises(BridgeError):
            self.bridge.recover(reviewed["fingerprint"], actor="operator", review="Reviewed")
        self.assertEqual(self.hc.count("send"), 1)
