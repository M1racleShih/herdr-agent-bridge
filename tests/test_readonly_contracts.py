"""Read-only contract checks use disposable source copies and fake version runners only."""

import shutil
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from herdr_agent_bridge import readiness
from herdr_agent_bridge import readonly_contracts as contracts

REPO = Path(__file__).resolve().parents[1]


def task(task_id, kind, answer, turn, parent=None):
    return {
        "submitted_text": contracts.PROMPTS["analysis"],
        "id": task_id,
        "parent": parent,
        "delivery": "received",
        "observed": "needs_human" if kind == "question" else "evidence_ready",
        "verified_success": False,
        "evidence": {
            "thread_id": "thread-1",
            "agent_turn_id": turn,
            "agent_completed": True,
            "result": {"task_id": task_id, "kind": kind, "answer": answer},
        },
    }


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-contract-tests-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        for name in contracts.REQUIRED_FILES:
            target = self.project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, target)
        self.sources = [
            self.quote(
                "src/herdr_agent_bridge/tools/smoke_hc.py",
                'for task in ["task-001", "followup-001"]:',
            ),
            self.quote(
                "tests/test_smoke_hc.py", 'self.assertIn("missing task evidence", result["error"])'
            ),
        ]
        self.analysis = task(
            "analysis-1",
            "result",
            {
                "initial_task_ids": ["task-001", "followup-001"],
                "blocked_error_kind": "blocked",
                "regression_test_names": [
                    "test_optimized_run_rejects_wrong_task_evidence",
                    "test_sigterm_during_cleanup_cannot_report_success",
                    "test_sigterm_reports_failure_and_attempts_cleanup",
                ],
                "success_requires_cleanup": True,
                "limitation": "An inert local fixture cannot establish a real agent or Grok integration.",
                "fixture_proves_real_agent": False,
                "fixture_proves_grok": False,
                "sources": self.sources,
            },
            "turn-1",
        )
        self.followup = task(
            "followup-1",
            "result",
            {
                "method_name": "test_optimized_run_rejects_wrong_task_evidence",
                "asserted_message": "missing task evidence",
                "previous_task_id": "analysis-1",
                "sources": self.sources,
            },
            "turn-2",
            "analysis-1",
        )
        self.question = task(
            "question-1", "question", {"question": "Which language?"}, "turn-3", "followup-1"
        )
        self.clarification = task(
            "clarification-1",
            "result",
            {
                "language": "English",
                "previous_question_task_id": "question-1",
                "done_verifies_success": False,
                "explanation": "Done is terminal state, not an independent check of the answer.",
            },
            "turn-4",
            "question-1",
        )

    def quote(self, path, text):
        lines = (self.project / path).read_text().splitlines()
        line = next(i for i, candidate in enumerate(lines, 1) if text in candidate)
        return {"path": path, "line": line, "quote": text}

    def checks(self, contract, current, previous=None):
        return contracts.check_task(contract, current, previous, self.project)

    def assert_passes(self, contract, current, previous=None):
        result = self.checks(contract, current, previous)
        self.assertTrue(result)
        self.assertTrue(all(item["passed"] is True for item in result), result)
        self.assertTrue(all(set(item) == {"check", "passed"} for item in result))

    def assert_fails(self, contract, current, previous=None):
        result = self.checks(contract, current, previous)
        self.assertTrue(result)
        self.assertFalse(all(item["passed"] is True for item in result), result)

    def test_legacy_rounds_verify_original_sources_and_reject_drift(self):
        current = self.project / contracts.SOURCE_PATHS[0]
        legacy = self.project / contracts.LEGACY_SOURCE_PATHS[0]
        legacy.parent.mkdir(exist_ok=True)
        legacy.write_text(current.read_text())
        analysis, followup = deepcopy(self.analysis), deepcopy(self.followup)
        for item in (analysis, followup):
            item["evidence"]["result"]["answer"]["sources"][0]["path"] = (
                contracts.LEGACY_SOURCE_PATHS[0]
            )
        rejected = contracts.check_task("analysis", analysis, project=self.project)
        self.assertFalse(all(check["passed"] for check in rejected))
        for item in (analysis, followup):
            item["submitted_text"] = item["submitted_text"].replace(
                contracts.SOURCE_PATHS[0], contracts.LEGACY_SOURCE_PATHS[0]
            )
        current.unlink()  # No current-layout fallback can manufacture success.
        for name, item, previous in (
            ("analysis", analysis, None),
            ("followup", followup, analysis),
        ):
            with self.subTest(contract=name):
                checks = contracts.check_task(name, item, previous=previous, project=self.project)
                self.assertTrue(all(check["passed"] for check in checks), checks)
        legacy.write_text(
            legacy.read_text().replace('"task-001", "followup-001"', '"changed", "followup-001"')
        )
        self.assertFalse(
            all(
                check["passed"]
                for check in contracts.check_task("analysis", analysis, project=self.project)
            )
        )

    def test_all_contracts_pass_with_correlated_evidence_without_mutation(self):
        before = deepcopy((self.analysis, self.followup, self.question, self.clarification))
        self.assert_passes("analysis", self.analysis)
        self.assert_passes("followup", self.followup, self.analysis)
        self.assert_passes("question", self.question, self.followup)
        self.assert_passes("clarification", self.clarification, self.question)
        self.assertEqual(before, (self.analysis, self.followup, self.question, self.clarification))
        self.assertIs(self.question["verified_success"], False)
        self.assertEqual(
            set(contracts.PROMPTS), {"analysis", "followup", "question", "clarification"}
        )

    def test_honest_analysis_passes_against_actual_repository(self):
        # Expected values are written out above, not derived from the verifier.
        result = contracts.check_task("analysis", self.analysis, project=REPO)
        self.assertTrue(all(item["passed"] is True for item in result), result)

    def test_analysis_rejects_false_facts_and_non_boolean_flags(self):
        false_fields = {
            "initial_task_ids": ["followup-001", "task-001"],
            "blocked_error_kind": "allowed",
            "regression_test_names": [],
            "success_requires_cleanup": 1,
            "limitation": "Everything worked completely.",
            "fixture_proves_real_agent": True,
            "fixture_proves_grok": 0,
        }
        for name, value in false_fields.items():
            with self.subTest(field=name):
                item = deepcopy(self.analysis)
                item["evidence"]["result"]["answer"][name] = value
                self.assert_fails("analysis", item)
        item = deepcopy(self.analysis)
        item["evidence"]["result"]["answer"]["limitation"] = (
            "The fixture proves a real agent and Grok integration."
        )
        self.assert_fails("analysis", item)

    def test_added_success_or_cleanup_assignment_fails_closed(self):
        source = self.project / "src/herdr_agent_bridge/tools/smoke_hc.py"
        original = source.read_text()
        for statement in ("result['passed'] = True", "clean = True"):
            with self.subTest(statement=statement):
                source.write_text(original + "\n" + statement + "\n")
                self.assert_fails("analysis", self.analysis)

    def test_source_drift_invalidates_stale_facts_and_ast_updates_expected_names(self):
        source = self.project / "src/herdr_agent_bridge/tools/smoke_hc.py"
        source.write_text(
            source.read_text().replace('"task-001", "followup-001"', '"new-task", "followup-001"')
        )
        changed = deepcopy(self.analysis)
        changed["evidence"]["result"]["answer"]["sources"][0] = self.quote(
            "src/herdr_agent_bridge/tools/smoke_hc.py", 'for task in ["new-task", "followup-001"]:'
        )
        self.assert_fails("analysis", changed)
        changed["evidence"]["result"]["answer"]["initial_task_ids"][0] = "new-task"
        self.assert_passes("analysis", changed)
        tests = self.project / "tests/test_smoke_hc.py"
        tests.write_text(tests.read_text() + "\ndef test_added_regression():\n    pass\n")
        self.assert_fails("analysis", changed)
        changed["evidence"]["result"]["answer"]["regression_test_names"].insert(
            0, "test_added_regression"
        )
        self.assert_passes("analysis", changed)

    def test_followup_extracts_assertion_and_rejects_stale_or_wrong_answer(self):
        for field, value in [
            ("method_name", "test_other"),
            ("asserted_message", "some error"),
            ("previous_task_id", "older-task"),
        ]:
            with self.subTest(field=field):
                changed = deepcopy(self.followup)
                changed["evidence"]["result"]["answer"][field] = value
                self.assert_fails("followup", changed, self.analysis)
        tests = self.project / "tests/test_smoke_hc.py"
        tests.write_text(
            tests.read_text().replace(
                'self.assertIn("missing task evidence", result["error"])',
                'self.assertIn("new assertion text", result["error"])',
            )
        )
        changed = deepcopy(self.followup)
        changed["evidence"]["result"]["answer"]["sources"][1] = self.quote(
            "tests/test_smoke_hc.py", 'self.assertIn("new assertion text", result["error"])'
        )
        self.assert_fails("followup", changed, self.analysis)
        changed["evidence"]["result"]["answer"]["asserted_message"] = "new assertion text"
        self.assert_passes("followup", changed, self.analysis)

    def test_citations_reject_wrong_lines_duplicates_traversal_and_bool_line(self):
        path, line = self.sources[0]["path"], self.sources[0]["line"]
        line_text = (self.project / path).read_text().splitlines()[line - 1]
        invalid_pairs = [
            [self.sources[0], self.sources[0]],
            [self.sources[0], {"path": path, "line": line, "quote": line_text.strip()}],
            [self.sources[0], {**self.sources[1], "line": True}],
            [self.sources[0], {**self.sources[1], "line": self.sources[1]["line"] + 1}],
            [self.sources[0], {**self.sources[1], "quote": "not an exact source quote"}],
            [self.sources[0], {**self.sources[1], "path": "tests/../tests/test_smoke_hc.py"}],
            [self.sources[0], {"path": "README.md", "line": 1, "quote": "herdr-agent-bridge"}],
            [self.sources[0], "not a citation"],
            [
                self.sources[0],
                self.sources[1],
                {"path": path, "line": -1, "quote": "invalid quote"},
            ],
        ]
        for sources in invalid_pairs:
            with self.subTest(sources=sources):
                changed = deepcopy(self.analysis)
                changed["evidence"]["result"]["answer"]["sources"] = sources
                self.assert_fails("analysis", changed)

    def test_symlink_source_or_source_directory_cannot_be_cited(self):
        source = self.project / "tests/test_smoke_hc.py"
        external = Path(self.temp.name) / "external.py"
        shutil.copyfile(source, external)
        source.unlink()
        source.symlink_to(external)
        self.assert_fails("analysis", self.analysis)
        source.unlink()
        tests = self.project / "tests"
        tests.rmdir()
        external_dir = Path(self.temp.name) / "external-dir"
        external_dir.mkdir()
        shutil.copyfile(external, external_dir / "test_smoke_hc.py")
        tests.symlink_to(external_dir, target_is_directory=True)
        self.assert_fails("analysis", self.analysis)

    def test_bad_current_results_fail_closed(self):
        corruptions = [
            lambda item: item.update(id=""),
            lambda item: item.update(delivery="submitted"),
            lambda item: item.update(observed="needs_human"),
            lambda item: item["evidence"].update(thread_id=""),
            lambda item: item["evidence"].update(agent_turn_id=None),
            lambda item: item["evidence"].update(agent_completed=1),
            lambda item: item["evidence"]["result"].update(task_id="stale"),
            lambda item: item["evidence"]["result"].update(kind="question"),
            lambda item: item["evidence"]["result"].update(answer=[]),
            lambda item: item.update(evidence=None),
        ]
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                item = deepcopy(self.analysis)
                corrupt(item)
                self.assert_fails("analysis", item)
        for malformed in (None, [], {}, "task"):
            with self.subTest(malformed=malformed):
                self.assert_fails("analysis", malformed)

    def test_previous_task_must_be_correlated_parent_same_thread_distinct_turn(self):
        for contract, current, previous in [
            ("followup", self.followup, self.analysis),
            ("clarification", self.clarification, self.question),
        ]:
            for what in (
                "wrong_parent",
                "different_thread",
                "same_turn",
                "missing_previous",
                "previous_result_id",
                "previous_incomplete",
                "previous_not_received",
            ):
                with self.subTest(contract=contract, what=what):
                    item, old = deepcopy(current), deepcopy(previous)
                    if what == "wrong_parent":
                        item["parent"] = "other"
                    elif what == "different_thread":
                        item["evidence"]["thread_id"] = "other"
                    elif what == "same_turn":
                        item["evidence"]["agent_turn_id"] = old["evidence"]["agent_turn_id"]
                    elif what == "missing_previous":
                        old = None
                    elif what == "previous_result_id":
                        old["evidence"]["result"]["task_id"] = "different"
                    elif what == "previous_incomplete":
                        old["evidence"]["agent_completed"] = False
                    else:
                        old["delivery"] = "unknown"
                    self.assert_fails(contract, item, old)

    def test_question_stays_unverified_and_clarification_matches_question(self):
        self.assert_passes("question", self.question)
        for field, value in [
            ("question", ""),
            ("question", []),
            ("question", "   "),
            ("question", "Which test should I run?"),
        ]:
            item = deepcopy(self.question)
            item["evidence"]["result"]["answer"][field] = value
            self.assert_fails("question", item)
        item = deepcopy(self.question)
        item["evidence"]["result"]["answer"]["question"] = "English or Chinese?"
        self.assert_passes("question", item)
        item["parent"] = "another-task"
        self.assert_fails("question", item, self.followup)
        item = deepcopy(self.question)
        item["verified_success"] = True
        self.assert_fails("question", item)
        self.assert_fails("clarification", self.clarification, item)
        for field, value in [
            ("language", "Chinese"),
            ("previous_question_task_id", "wrong"),
            ("done_verifies_success", 0),
            ("explanation", "  "),
        ]:
            item = deepcopy(self.clarification)
            item["evidence"]["result"]["answer"][field] = value
            self.assert_fails("clarification", item, self.question)

    def test_unknown_contract_always_raises_value_error(self):
        for value in ("unknown", None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.checks(value, self.analysis)

    def test_probe_isolated_versions_and_bounded_output(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            self.assertEqual(args[1:], ["--version"])
            self.assertEqual(kwargs["cwd"], kwargs["env"]["HOME"])
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(Path(kwargs["cwd"]) / "codex"))
            self.assertNotIn("HC_SESSION", kwargs["env"])
            self.assertNotIn("CODEX_SESSION_ID", kwargs["env"])
            self.assertEqual(kwargs["timeout"], 15)
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertFalse(kwargs["check"])
            return SimpleNamespace(
                returncode=0,
                stdout=args[0] + " 1.2.3\x1b" + "x" * 500 + "\nsecret second line",
                stderr="secret stderr",
            )

        result = readiness.probe(self.project, runner)
        self.assertTrue(result["ready"], result)
        self.assertEqual([args for args, _ in calls], [["hc", "--version"], ["codex", "--version"]])
        self.assertIn("real Grok execution", result["not_verified"])
        self.assertIn("coding agent authentication", result["not_verified"])
        for check in result["checks"][-2:]:
            self.assertEqual(check["returncode"], 0)
            self.assertLessEqual(len(check["version"]), 160)
            self.assertNotIn("\n", check["version"])
            self.assertNotIn("\x1b", check["version"])
            self.assertNotIn("secret", str(check))
        self.assertFalse(Path(calls[0][1]["cwd"]).exists())

    def test_probe_accepts_standalone_source_without_harness_documents(self):
        self.assertFalse((self.project / "docs/ROADMAP.md").exists())
        self.assertFalse((self.project / "docs/decisions").exists())

        def runner(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=args[0] + " fixture", stderr="")

        self.assertTrue(readiness.probe(self.project, runner)["ready"])

    def test_probe_fails_closed_on_timeout_errors_and_missing_files(self):
        def runner(args, **kwargs):
            if args[0] == "hc":
                raise subprocess.TimeoutExpired(args, 15, output="secret timeout data")
            raise RuntimeError("secret exception data")

        (self.project / "AGENTS.md").unlink()
        result = readiness.probe(self.project, runner)
        self.assertFalse(result["ready"])
        checks = {item["check"]: item for item in result["checks"]}
        self.assertFalse(checks["required_file:AGENTS.md"]["passed"])
        self.assertEqual(checks["hc_version"]["error"], "timeout")
        self.assertEqual(checks["codex_version"]["error"], "unexpected_error")
        self.assertNotIn("secret", str(result))

    def test_probe_records_nonzero_and_not_found_without_stderr(self):
        def runner(args, **kwargs):
            if args[0] == "hc":
                return SimpleNamespace(returncode=42, stdout="hc failed\nprivate", stderr="private")
            raise FileNotFoundError("private path")

        result = readiness.probe(self.project, runner)
        checks = {item["check"]: item for item in result["checks"]}
        self.assertFalse(result["ready"])
        self.assertEqual(checks["hc_version"]["returncode"], 42)
        self.assertFalse(checks["hc_version"]["passed"])
        self.assertEqual(checks["codex_version"]["error"], "not_found")
        self.assertNotIn("private", str(result))

    def test_probe_handles_malformed_version_results_and_isolation_failure(self):
        def runner(args, **kwargs):
            if args[0] == "hc":
                return SimpleNamespace(returncode=True, stdout="hc 1.2")
            return SimpleNamespace(returncode=0, stdout="")

        result = readiness.probe(self.project, runner)
        checks = {item["check"]: item for item in result["checks"]}
        self.assertFalse(result["ready"])
        self.assertEqual(checks["hc_version"]["error"], "invalid_returncode")
        self.assertFalse(checks["codex_version"]["passed"])
        with patch.object(
            readiness.tempfile, "TemporaryDirectory", side_effect=RuntimeError("secret path")
        ):
            failed = readiness.probe(self.project, runner)
        self.assertFalse(failed["ready"])
        versions = [item for item in failed["checks"] if item["check"].endswith("_version")]
        self.assertEqual(len(versions), 2)
        self.assertTrue(
            all(item["error"] == "isolated_environment_unavailable" for item in versions)
        )
        self.assertNotIn("secret", str(failed))


if __name__ == "__main__":
    unittest.main()
