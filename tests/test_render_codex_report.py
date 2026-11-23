"""Offline Codex reports preserve cleanup evidence semantics."""

import unittest

from herdr_agent_bridge.tools.render_codex_report import cleanup_state, render, source_hashes


class CleanupReportTests(unittest.TestCase):
    def test_report_reproduction_uses_installed_cli(self):
        html = render({}, "a" * 64, None)
        self.assertIn("herdr-bridge tool accept_codex", html)
        self.assertIn("herdr-bridge tool render_codex_report", html)
        self.assertNotIn("python3 src/", html)

    def test_source_hashes_support_current_and_historical_report_inputs(self):
        for paths in (
            ("docs/PROJECT.md", "scripts/bridge_protocol.json"),
            ("docs/ROADMAP.md", "docs/decisions/0001-herdr-core-first.md"),
        ):
            with self.subTest(paths=paths):
                rendered = source_hashes({"source_sha256": {path: "a" * 64 for path in paths}})
                for path in paths:
                    self.assertIn(path, rendered)
                self.assertIn("aaaaaaaaaaaa", rendered)

    def test_source_hashes_do_not_publish_unrecognized_paths_or_malformed_hashes(self):
        rendered = source_hashes(
            {"source_sha256": {"/private/secret": "a" * 64, "docs/PROJECT.md": "<private-value>"}}
        )
        self.assertNotIn("/private/secret", rendered)
        self.assertNotIn("private-value", rendered)

    def test_normal_owned_cleanup_passes(self):
        records = [
            {"args": ["kill", "own-terminal"], "passed": True},
            {"args": ["rm", "own-terminal"], "passed": True},
            {"args": ["server", "stop"], "passed": True},
        ]
        self.assertEqual(cleanup_state({"cleanup": records}), "pass")

    def test_confirmed_absent_terminal_and_stopped_core_pass(self):
        records = [
            {"args": ["get", "own-terminal"], "passed": True, "reconciled": "absent"},
            {"args": ["server", "stop"], "passed": True},
        ]
        self.assertEqual(cleanup_state({"cleanup": records}), "pass")

    def test_three_unrelated_successes_cannot_prove_cleanup(self):
        records = [{"args": ["read", "own-terminal"], "passed": True} for _ in range(3)]
        self.assertNotEqual(cleanup_state({"cleanup": records}), "pass")

    def test_different_terminal_ids_cannot_prove_owned_cleanup(self):
        records = [
            {"args": ["kill", "one"], "passed": True},
            {"args": ["rm", "another"], "passed": True},
            {"args": ["server", "stop"], "passed": True},
        ]
        self.assertNotEqual(cleanup_state({"cleanup": records}), "pass")

    def test_failure_and_missing_evidence_remain_distinct(self):
        self.assertEqual(
            cleanup_state({"cleanup": [{"args": ["server", "stop"], "passed": False}]}), "fail"
        )
        for records in (
            [],
            [{"args": ["get", "own-terminal"], "passed": True}],
            [
                {"args": ["get", "own-terminal"], "passed": True},
                {"args": ["server", "stop"], "passed": True},
            ],
        ):
            self.assertEqual(cleanup_state({"cleanup": records}), "missing")


if __name__ == "__main__":
    unittest.main()
