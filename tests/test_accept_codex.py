"""Acceptance must not survive failed or interrupted cleanup as a green result."""

import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_agent_bridge.tools import accept_codex as acceptance
from herdr_agent_bridge.tools import acceptance_common


class Cleanup:
    def __init__(self, action):
        self.action = action

    def close(self):
        return self.action()


class AcceptanceChecks(unittest.TestCase):
    def setUp(self):
        selected = patch.object(acceptance, "PROJECT", Path(__file__).resolve().parents[1])
        selected.start()
        self.addCleanup(selected.stop)

    def finish(self, action):
        with tempfile.TemporaryDirectory(prefix="hab-accept-test-") as directory:
            output = Path(directory) / "result.json"
            report = {"passed": True, "phase": "running"}
            acceptance_common.finish_report(Cleanup(action), report, output)
            self.assertEqual(json.loads(output.read_text()), report)
            self.assertEqual(report["phase"], "finished")
            return report

    def test_signal_during_cleanup_cannot_pass_and_restores_handlers(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum):
                prior = signal.getsignal(signum)

                def cleanup():
                    os.kill(os.getpid(), signum)
                    return [{"passed": True}]

                report = self.finish(cleanup)
                self.assertFalse(report["passed"])
                self.assertIn(signum, report["signals_during_cleanup"])
                self.assertEqual(signal.getsignal(signum), prior)

    def test_cleanup_failure_or_exception_cannot_pass(self):
        def fail():
            raise RuntimeError("fixture cleanup failed")

        for action in (lambda: [{"passed": False}], lambda: [], fail):
            with self.subTest(action=action):
                self.assertFalse(self.finish(action)["passed"])

    def test_successful_cleanup_preserves_verified_result(self):
        self.assertTrue(self.finish(lambda: [{"passed": True}])["passed"])

    def test_duplicate_or_wrong_source_quotations_cannot_pass(self):
        path = "src/herdr_agent_bridge/tools/smoke_hc.py"
        quote = (acceptance.PROJECT / path).read_text().splitlines()[0]
        source = {"path": path, "line": 1, "quote": quote}
        checks = acceptance.quote_checks({"sources": [source, source]})
        self.assertFalse(all(c["passed"] for c in checks))
        wrong = {"path": path, "line": 1, "quote": "wrong source quotation"}
        checks = acceptance.quote_checks({"sources": [source, wrong]})
        self.assertFalse(all(c["passed"] for c in checks))


if __name__ == "__main__":
    unittest.main()
