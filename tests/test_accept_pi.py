"""Acceptance must prove successful source reads in the intended native turn."""

import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from herdr_agent_bridge.tools import accept_pi as acceptance


def user(identity):
    return {"id": identity, "message": {"role": "user"}}


def call(identity, path):
    return {
        "message": {
            "role": "assistant",
            "content": [
                {"type": "toolCall", "id": identity, "name": "read", "arguments": {"path": path}}
            ],
        }
    }


def result(identity, error=False):
    return {
        "message": {
            "role": "toolResult",
            "toolName": "read",
            "toolCallId": identity,
            "isError": error,
        }
    }


class PiAcceptanceChecks(unittest.TestCase):
    def test_success_requires_matching_tool_call_and_successful_result(self):
        with tempfile.TemporaryDirectory() as directory:
            events = [
                user("analysis"),
                call("a", "src/herdr_agent_bridge/tools/smoke_hc.py"),
                result("a"),
                call("b", str(Path(directory) / "tests/test_smoke_hc.py")),
                result("b"),
            ]
            self.assertEqual(
                acceptance.source_reads(events, "analysis", directory),
                {"src/herdr_agent_bridge/tools/smoke_hc.py", "tests/test_smoke_hc.py"},
            )

    def test_failed_unrelated_or_unmatched_reads_are_not_source_evidence(self):
        events = [
            user("analysis"),
            call("failed", "src/herdr_agent_bridge/tools/smoke_hc.py"),
            result("failed", error=True),
            call("unrelated", "README.md"),
            result("unrelated"),
            result("unmatched"),
            call("no-result", "tests/test_smoke_hc.py"),
        ]
        self.assertEqual(acceptance.source_reads(events, "analysis", "/tmp/fixture"), set())

    def test_reads_in_another_user_turn_cannot_support_analysis(self):
        events = [
            user("earlier"),
            call("old", "src/herdr_agent_bridge/tools/smoke_hc.py"),
            result("old"),
            user("analysis"),
            call("incomplete", "src/herdr_agent_bridge/tools/smoke_hc.py"),
            user("followup"),
            result("incomplete"),
            call("late", "tests/test_smoke_hc.py"),
            result("late"),
        ]
        self.assertEqual(acceptance.source_reads(events, "analysis", "/tmp/fixture"), set())
        self.assertEqual(acceptance.source_reads(events, "missing", "/tmp/fixture"), set())

    def test_partial_manager_setup_still_closes_without_bridge_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SimpleNamespace(
                close=Mock(return_value={"closed": True, "cleanup": []}),
                bridge=Mock(side_effect=AssertionError("no bridge exists")),
            )
            with patch.object(acceptance, "Manager", return_value=manager):
                checks = acceptance.Cleanup(Path(directory)).close()
            manager.close.assert_called_once_with()
            manager.bridge.assert_not_called()
            self.assertTrue(checks and all(item["passed"] for item in checks))

    def test_failed_run_native_evidence_can_be_preserved_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root, scratch = Path(directory) / "run", Path(directory) / "scratch"
            (root / "bridge").mkdir(parents=True)
            (root / "bridge" / "journal.json").write_text(json.dumps({"scratch": str(scratch)}))
            sessions = scratch / "pi" / "sessions"
            sessions.mkdir(parents=True)
            # An incomplete last record must survive a failed observation too.
            (sessions / "native.jsonl").write_bytes(b'{"type":"session"}\n{"partial":')
            (scratch / "pi" / "auth.json").write_text("PRIVATE-FIXTURE-AUTH")
            self.assertEqual(acceptance.capture_native_sessions(root), ["pi-session.jsonl"])
            preserved = root / "pi-session.jsonl"
            self.assertEqual(preserved.read_bytes(), (sessions / "native.jsonl").read_bytes())
            self.assertEqual(stat.S_IMODE(preserved.stat().st_mode), 0o600)
            self.assertFalse((root / "auth.json").exists())

    def test_native_snapshot_goes_to_separate_acceptance_evidence_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root, scratch, evidence = (
                Path(directory) / part for part in ("run", "scratch", "evidence")
            )
            (root / "bridge").mkdir(parents=True)
            evidence.mkdir(mode=0o700)
            (root / "bridge/journal.json").write_text(json.dumps({"scratch": str(scratch)}))
            sessions = scratch / "pi/sessions"
            sessions.mkdir(parents=True)
            (sessions / "owned.jsonl").write_text('{"type":"session"}\n')
            self.assertEqual(
                acceptance.capture_native_sessions(root, evidence), ["pi-session.jsonl"]
            )
            self.assertTrue((evidence / "pi-session.jsonl").is_file())
            self.assertFalse((root / "pi-session.jsonl").exists())

    def test_capture_never_follows_session_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root, scratch = Path(directory) / "run", Path(directory) / "scratch"
            (root / "bridge").mkdir(parents=True)
            (root / "bridge" / "journal.json").write_text(json.dumps({"scratch": str(scratch)}))
            (scratch / "pi").mkdir(parents=True)
            unrelated = Path(directory) / "unrelated"
            unrelated.mkdir()
            (unrelated / "native.jsonl").write_text("PRIVATE-FIXTURE-OTHER-SESSION")
            (scratch / "pi" / "sessions").symlink_to(unrelated, target_is_directory=True)
            with self.assertRaises(acceptance.BridgeError):
                acceptance.capture_native_sessions(root)
            self.assertFalse((root / "pi-session.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
