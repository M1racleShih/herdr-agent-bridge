"""Regression checks for the verifier; use a fake hc, never a real core."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/smoke_hc.py"
FAKE = """from pathlib import Path
import json, os, signal, sys
command = sys.argv[1]
mode = os.environ.get("HAB_TEST_MODE")
state_file = Path(__file__).with_name("state.json")
if command == "--version":
    print("hc 0.1.17")
elif command == "run":
    if mode == "terminate":
        os.kill(os.getppid(), signal.SIGTERM)
    print(json.dumps({"address": "t1"}))
elif command == "wait":
    print(json.dumps({"status": "idle", "turn": 0}))
elif command == "send":
    task = sys.argv[-1]
    if task == "must-not-arrive":
        print(json.dumps({"error": {"kind": "blocked"}}), file=sys.stderr)
        sys.exit(9)
    state = json.loads(state_file.read_text()) if state_file.exists() else {"turn": 0}
    state.update({"task_id": task, "turn": state["turn"] + 1})
    state_file.write_text(json.dumps(state))
    status = "blocked" if task == "block" else "done"
    print(json.dumps({"started": True, "wait": {"status": status, "turn": state["turn"]}}))
elif command == "read":
    task = json.loads(state_file.read_text())["task_id"] if mode == "terminate-cleanup" else "wrong-task"
    print("BRIDGE_RESULT " + json.dumps({"task_id": task, "evidence": "fixture"}))
elif command == "get":
    print(json.dumps({"status": {"state": "blocked"}}))
else:
    if command == "kill" and mode == "terminate-cleanup":
        os.kill(os.getppid(), signal.SIGTERM)
    print("{}")
"""


@unittest.skipUnless(
    os.name == "posix" and sys.platform in {"linux", "darwin"}, "POSIX smoke check"
)
class SmokeVerifierTests(unittest.TestCase):
    def run_scenario(self, mode):
        with tempfile.TemporaryDirectory(prefix="hab-regression-") as directory:
            root = Path(directory)
            fake = root / "hc"
            fake.write_text("#!" + sys.executable + "\n" + FAKE)
            fake.chmod(0o700)
            env = os.environ.copy()
            env["PATH"] = directory + os.pathsep + env["PATH"]
            env["HAB_TEST_MODE"] = mode
            report = root / "report.json"
            p = subprocess.run(
                [sys.executable, "-O", str(SCRIPT), "--output", str(report)],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            result = json.loads(report.read_text())
            try:
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertFalse(result["passed"])
                self.assertEqual(result["phase"], "finished")
                self.assertEqual(len(result["checks"]), 3 if mode == "terminate-cleanup" else 0)
                self.assertEqual(len(result["cleanup"]), 3)
                self.assertTrue(all(c.get("exit_code") == 0 for c in result["cleanup"]))
                self.assertTrue(result["session"].startswith("smoke-"))
                return result
            finally:
                scratch = Path(result["scratch"]).resolve()
                if scratch.parent == Path("/tmp").resolve() and scratch.name.startswith("hab-hc-"):
                    shutil.rmtree(scratch)

    def test_optimized_run_rejects_wrong_task_evidence(self):
        result = self.run_scenario("mismatch")
        self.assertIn("missing task evidence", result["error"])

    def test_sigterm_reports_failure_and_attempts_cleanup(self):
        result = self.run_scenario("terminate")
        self.assertIn("received signal", result["error"])

    def test_sigterm_during_cleanup_cannot_report_success(self):
        result = self.run_scenario("terminate-cleanup")
        self.assertIn("received signal during cleanup", result["error"])


if __name__ == "__main__":
    unittest.main()
