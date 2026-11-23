"""Real disposable checks with fake agents; no model or external service access."""

import json
import socket
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from pathlib import Path

import test_development
from test_pi_bridge import FakeHC

from herdr_agent_bridge import development
from herdr_agent_bridge.agent_bridge import BridgeError
from herdr_agent_bridge.project_bridge import Project


class VerificationConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-verification-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "code.py").write_text("value = 1\n")
        source = self.root / ".artifacts" / "source"
        source.mkdir(parents=True)
        (source / "settings.json").write_text("{}")
        (source / "auth.json").write_text("{}")
        self.socket = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.socket.close)
        address = str(self.root / ".artifacts" / "check.sock")
        self.socket.bind(address)
        self.socket.listen(1)
        self.socket.settimeout(3)
        script = (
            "import socket; from code import value; "
            f"s=socket.socket(socket.AF_UNIX); s.connect({address!r}); "
            "s.sendall(b'ready'); s.recv(1); s.close(); assert value == 2"
        )
        self.config = self.root / ".artifacts" / "project.json"
        Project.initialize(
            self.config,
            "fixture",
            self.root,
            {"bot_id": "fixture", "conversation_id": "fixture"},
            runtime="isolated",
            checks=[{"name": "unit", "argv": [sys.executable, "-c", script], "timeout": 10}],
        )
        self.hc = FakeHC()
        self.entry = self.project()
        self.entry.start(
            "request",
            {
                "task_id": "fix-value",
                "goal": "Set value to two",
                "allowed_paths": ["code.py"],
                "acceptance": ["value equals two"],
                "checks": ["unit"],
            },
            source_home=source,
        )
        self.addCleanup(self.entry.close)
        run = self.root / self.entry.record()[1]["run"] / "bridge"
        (run / "pi-selection.json").write_text(
            json.dumps(
                {
                    "state": "ready",
                    "model": "openai/gpt-6.1-sol",
                    "thinking": "high",
                    "session_id": "pi-session-01",
                }
            )
        )
        self.entry.submit("round-one", "Implement the fix")
        bridge = self.entry.manager(self.entry.record()[1]).bridge()
        fixture = test_development.DevelopmentEvidenceTests()
        fixture.project = self.root
        entries = fixture.native_history(
            bridge, "round-one", bridge.task("round-one")["submitted_text"], {"summary": "fixed"}
        )
        history = Path(bridge.data["scratch"]) / "pi" / "sessions" / "session.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text("".join(json.dumps(item) + "\n" for item in entries))
        self.hc.terminal.update(turn=1, status={"state": "done"})
        self.entry.status(observe=True)
        (self.root / "code.py").write_text("value = 2\n")

    def project(self):
        return Project(self.config, runner=self.hc)

    @contextmanager
    def running_check(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            verification = pool.submit(self.project().verify)
            connection, _ = self.socket.accept()
            with connection:
                self.assertEqual(connection.recv(5), b"ready")
                try:
                    yield pool, verification
                finally:
                    try:
                        connection.sendall(b"x")
                    except BrokenPipeError:
                        pass

    def test_status_remains_available_while_an_external_check_is_running(self):
        with self.running_check() as (pool, verification):
            status = pool.submit(self.project().status)
            try:
                result = status.result(timeout=1)
            except TimeoutError:
                self.fail("status was blocked by the running verification")
            self.assertEqual(result["request_id"], "request")
        self.assertTrue(verification.result(timeout=3)["passed"])

    def test_followup_is_refused_without_sending_while_verification_runs(self):
        with self.running_check():
            with self.assertRaisesRegex(BridgeError, "verification"):
                self.project().submit("round-two", "Continue", parent="round-one")
            self.assertEqual(self.hc.count("send"), 1)

    def test_takeover_cancels_checks_and_never_saves_a_passing_verification(self):
        with self.running_check() as (pool, verification):
            takeover = pool.submit(self.project().control, "takeover", actor="reviewer")
            self.assertEqual(takeover.result(timeout=1)["development"]["control"], "operator")
            try:
                result = verification.result(timeout=2)
            except TimeoutError:
                self.fail("takeover did not stop the active check")
            self.assertFalse(result["passed"])
            self.assertEqual(result["reason"], "verification_cancelled")
            self.assertIsNone(self.project().status()["development"]["verification"])

    def test_close_requests_cancellation_before_cleaning_the_agent(self):
        with self.running_check() as (pool, verification):
            closing = pool.submit(self.project().close)
            result = closing.result(timeout=1)
            self.assertFalse(result["closed"])
            self.assertEqual(self.hc.count("kill"), 0)
            self.assertFalse(result["cleanup"][0]["passed"])
            self.assertFalse(verification.result(timeout=2)["passed"])
        self.assertTrue(self.project().close()["closed"])

    def test_reconcile_resolves_an_interrupted_verification_without_replaying_checks(self):
        bridge = self.entry.manager(self.entry.record()[1]).bridge()
        bridge.begin_verification()  # Durable intent left behind by a stopped verifier.
        with self.assertRaisesRegex(BridgeError, "verification"):
            self.project().verify()
        result = self.project().control("reconcile")
        job = result["development"]["verification_job"]
        self.assertEqual((job["state"], job["reason"]), ("failed", "verification_interrupted"))
        self.assertIsNone(result["development"]["verification"])
        self.assertEqual(self.hc.count("send"), 1)

    def test_duplicate_verification_is_refused_while_the_original_check_runs(self):
        with self.running_check() as (pool, verification):
            duplicate = pool.submit(self.project().verify)
            with self.assertRaisesRegex(BridgeError, "verification"):
                duplicate.result(timeout=1)
        self.assertTrue(verification.result()["passed"])

    def test_code_changed_during_checks_is_never_accepted(self):
        with self.running_check() as (_, verification):
            (self.root / "code.py").write_text("value = 3\n")
        result = verification.result()
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "verification_superseded")
        with self.assertRaises(BridgeError):
            self.project().decide("accept", "reviewer", "must not accept stale code")

    def test_native_history_changed_during_checks_invalidates_the_result(self):
        with self.running_check() as (_, verification):
            bridge = self.entry.manager(self.entry.record()[1]).bridge()
            history = Path(bridge.data["scratch"]) / "pi" / "sessions" / "session.jsonl"
            with history.open("a") as stream:
                stream.write("\n")
        self.assertFalse(verification.result()["passed"])
        self.assertIsNone(self.project().status()["development"]["verification"])

    def test_inherited_child_lease_prevents_recovery_until_the_child_has_exited(self):
        bridge = self.entry.manager(self.entry.record()[1]).bridge()
        with development.verification_lease(bridge.root) as lease:
            bridge.begin_verification()
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('ready', flush=True); sys.stdin.buffer.read(1)",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                pass_fds=(lease.fileno(),),
            )
        try:
            self.assertEqual(child.stdout.readline(), b"ready\n")
            result = self.project().control("reconcile")
            self.assertEqual(result["development"]["verification_job"]["state"], "running")
            with self.assertRaisesRegex(BridgeError, "verification"):
                self.project().verify()
        finally:
            child.communicate(b"x", timeout=3)
        result = self.project().control("reconcile")
        self.assertEqual(
            result["development"]["verification_job"]["reason"], "verification_interrupted"
        )
