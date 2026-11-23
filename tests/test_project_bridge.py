"""Persistent project entry contracts; disposable projects and fake hc only."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from herdr_agent_bridge import project_bridge
from herdr_agent_bridge.agent_bridge import BridgeError

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

OWNER = {"bot_id": "project-bot", "conversation_id": "project-conversation"}
CHECKS = [{"name": "value", "argv": [sys.executable, "-c", "assert True"], "timeout": 10}]


class ProjectEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-project-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "code.py").write_text("value = 1\n")
        self.config = self.root / ".artifacts" / "project.json"

    def init(self, **kwargs):
        kwargs.setdefault(
            "runtime", "isolated"
        )  # These are acceptance fixtures, not daily sessions.
        return project_bridge.Project.initialize(
            self.config, "test-project", self.root, OWNER, checks=CHECKS, **kwargs
        )

    def source(self):
        source = self.root / ".artifacts" / "pi-source"
        source.mkdir()
        (source / "settings.json").write_text("{}")
        (source / "auth.json").write_text("{}")
        return source

    def test_new_run_uses_purpose_directory_and_repeated_start_preserves_identity(self):
        from test_pi_bridge import FakeHC

        self.init()
        entry = project_bridge.Project(self.config, runner=FakeHC())
        first = entry.start("purpose-run", source_home=self.source())
        self.addCleanup(entry.close, "purpose-run")
        self.assertTrue(first["run"].startswith(".artifacts/runs/"))
        before = self.config.read_bytes()
        self.assertEqual(entry.start("purpose-run")["run_id"], first["run_id"])
        self.assertEqual(self.config.read_bytes(), before)

    def test_shared_starts_keep_the_original_legacy_run_until_close(self):
        from test_pi_bridge import FakeHC

        self.init()
        old_hc, new_hc = FakeHC(), FakeHC()
        old = project_bridge.Project(self.config, runner=old_hc)
        source = self.source()
        started = old.start("original", source_home=source)
        self.addCleanup(old.close, "original")
        new_config = Path(self.temp.name) / "caller.json"
        project_bridge.Project.initialize(
            new_config,
            "other-alias",
            self.root,
            {"bot_id": "other-bot", "conversation_id": "other-conversation"},
            runtime="isolated",
        )
        before = self.config.read_bytes(), new_config.read_bytes()

        project_bridge.Project.enable_shared_starts(self.config, new_config)
        new = project_bridge.Project(new_config, runner=new_hc)
        with self.assertRaises(BridgeError) as conflict:
            new.start("independent")
        self.assertEqual(conflict.exception.code, "project_start_conflict")
        self.assertNotIn("original", str(conflict.exception))
        self.assertEqual((self.config.read_bytes(), new_config.read_bytes()), before)
        self.assertEqual(old.start("original")["run_id"], started["run_id"])
        self.assertEqual(old_hc.count("run"), 1)
        self.assertEqual(new_hc.count("run"), 0)
        self.assertTrue(old.close("original")["closed"])
        self.assertFalse(new.start("independent", source_home=source)["closed"])
        self.addCleanup(new.close, "independent")
        self.assertEqual(new_hc.count("run"), 1)

    def test_shared_starts_keep_unresolved_legacy_creation_from_making_another_run(self):
        from test_pi_bridge import FakeHC

        self.init()
        old_hc, new_hc = FakeHC(), FakeHC()
        old = project_bridge.Project(self.config, runner=old_hc)
        collision = self.root / ".artifacts/runs/test-project-fixed"
        collision.mkdir(parents=True)
        with patch("secrets.token_hex", return_value="fixed"), self.assertRaises(FileExistsError):
            old.start("unknown", source_home=self.source())
        new_config = Path(self.temp.name) / "caller.json"
        project_bridge.Project.initialize(
            new_config, "second", self.root, OWNER, runtime="isolated"
        )

        project_bridge.Project.enable_shared_starts(self.config, new_config)
        new = project_bridge.Project(new_config, runner=new_hc)
        with self.assertRaises(BridgeError) as blocked:
            new.start("second")
        self.assertEqual(blocked.exception.code, "project_admission_unresolved")
        self.assertEqual(old.configuration()["active_request"], "unknown")
        self.assertIsNone(new.configuration()["active_request"])
        self.assertEqual(old_hc.count("run") + new_hc.count("run"), 0)
        self.assertEqual(list(collision.parent.iterdir()), [collision])

    def test_shared_starts_need_no_roster_or_previous_owner_config_after_close(self):
        from test_pi_bridge import FakeHC

        self.init()
        project_bridge.Project.enable_shared_starts(self.config)
        source = self.source()
        old = project_bridge.Project(self.config, runner=FakeHC())
        prior = old.start("prior", source_home=source)
        self.assertTrue(old.close("prior")["closed"])
        self.config.rename(self.config.with_suffix(".retired"))

        new_config = Path(self.temp.name) / "new-caller.json"
        project_bridge.Project.initialize(new_config, "new", self.root, OWNER, runtime="isolated")
        new = project_bridge.Project(new_config, runner=FakeHC())
        started = new.start("new-request", source_home=source)
        self.addCleanup(new.close, "new-request")
        self.assertNotEqual(started["run_id"], prior["run_id"])

    def development_spec(self):
        return {
            "task_id": "change-value",
            "goal": "Change the value",
            "allowed_paths": ["code.py"],
            "acceptance": ["Value equals two"],
            "checks": ["value"],
        }

    def test_operator_can_terminate_a_development_task_after_its_run_is_closed(self):
        from test_pi_bridge import FakeHC

        self.init()
        source = self.source()
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        original = entry.start("original", self.development_spec(), source_home=source)
        self.assertTrue(entry.close("original")["closed"])
        calls_before = list(hc.calls)
        for decision in ("accept", "reject"):
            with self.assertRaisesRegex(BridgeError, "manager is closed"):
                entry.decide(decision, "operator", "Run has been closed", "original")
        recovered = entry.decide("terminate", "operator", "Run has been closed", "original")
        self.assertEqual(recovered["development"]["state"], "terminated")
        self.assertEqual(recovered["development"]["history"][-1]["actor"], "operator")
        self.assertTrue(recovered["closed"])
        self.assertEqual(recovered["run_id"], original["run_id"])
        self.assertEqual(hc.calls, calls_before)
        future = project_bridge.Project(self.config, runner=FakeHC())
        self.assertFalse(
            future.start("future", self.development_spec(), source_home=source)["closed"]
        )
        self.addCleanup(future.close, "future")

    def test_operator_can_abandon_a_partial_development_start_blocked_by_the_closed_original(self):
        from test_pi_bridge import FakeHC

        self.init()
        source = self.source()
        original_entry = project_bridge.Project(self.config, runner=FakeHC())
        original_entry.start("original", self.development_spec(), source_home=source)
        self.assertTrue(original_entry.close("original")["closed"])
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        with self.assertRaisesRegex(BridgeError, "one active development task"):
            entry.start("refused", self.development_spec(), source_home=source)
        self.assertEqual(entry.configuration()["active_request"], "refused")
        refused_run = self.root / json.loads(self.config.read_text())["starts"]["refused"]["run"]
        manager_before = (refused_run / "manager.json").read_bytes()
        abandoned = entry.abandon_start("refused")
        self.assertEqual(abandoned["delivery"], "not_attempted")
        self.assertEqual((refused_run / "manager.json").read_bytes(), manager_before)
        self.assertIsNone(entry.configuration()["active_request"])
        with self.assertRaises(BridgeError) as repeated:
            entry.start("refused", self.development_spec(), source_home=source)
        self.assertEqual(repeated.exception.code, "project_start_abandoned")
        self.assertEqual(hc.calls, [])
        entry.decide("terminate", "operator", "Original QA task ended", "original")
        self.assertEqual(entry.abandon_start("refused")["delivery"], "not_attempted")
        self.assertFalse(
            entry.start("future", self.development_spec(), source_home=source)["closed"]
        )
        self.addCleanup(entry.close, "future")
        self.assertEqual(hc.count("run"), 1)

    def test_operator_cannot_abandon_a_partial_start_with_bridge_creation_evidence(self):
        from test_pi_bridge import FakeHC

        self.init()
        source = self.source()
        original = project_bridge.Project(self.config, runner=FakeHC())
        original.start("original", self.development_spec(), source_home=source)
        self.assertTrue(original.close("original")["closed"])
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        with self.assertRaisesRegex(BridgeError, "one active development task"):
            entry.start("uncertain", self.development_spec(), source_home=source)
        before = self.config.read_bytes()
        run = self.root / json.loads(before)["starts"]["uncertain"]["run"]
        (run / "bridge").mkdir()  # Simulate an incomplete Bridge creation, even without a journal.
        with self.assertRaisesRegex(BridgeError, "run has creation evidence"):
            entry.abandon_start("uncertain")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(entry.configuration()["active_request"], "uncertain")
        self.assertEqual(hc.calls, [])

    def test_operator_cannot_assume_any_partial_development_start_was_blocked(self):
        from test_pi_bridge import FakeHC

        self.init()
        source = self.source()
        original = project_bridge.Project(self.config, runner=FakeHC())
        original.start("original", self.development_spec(), source_home=source)
        self.assertTrue(original.close("original")["closed"])
        entry = project_bridge.Project(self.config, runner=FakeHC())
        with self.assertRaisesRegex(BridgeError, "one active development task"):
            entry.start("uncertain", self.development_spec(), source_home=source)
        entry.decide("terminate", "operator", "Original QA task ended", "original")
        # The initial Manager record is insufficient without the still-blocking guard.
        before = self.config.read_bytes()
        with self.assertRaisesRegex(BridgeError, "run has creation evidence"):
            entry.abandon_start("uncertain")
        self.assertEqual(self.config.read_bytes(), before)

    def test_shared_starts_release_closed_run_with_large_development_journal(self):
        from test_pi_bridge import FakeHC

        self.init()
        project_bridge.Project.enable_shared_starts(self.config)
        source = self.source()
        (self.root / "code.py").write_text("#" + "x" * (5 * 1024 * 1024) + "\n")
        old = project_bridge.Project(self.config, runner=FakeHC())
        started = old.start(
            "large",
            {
                "task_id": "change-value",
                "goal": "Change the value",
                "allowed_paths": ["code.py"],
                "acceptance": ["Value equals two"],
                "checks": ["value"],
            },
            source_home=source,
        )
        self.addCleanup(old.close, "large")
        run = self.root / started["run"]
        self.assertGreater((run / "bridge/journal.json").stat().st_size, 4 * 1024 * 1024)
        old.decide("terminate", "test-operator", "Fixture finished")
        self.assertTrue(old.close("large")["closed"])
        receipt = run / "run-closed.json"
        self.assertLess(receipt.stat().st_size, 1024)
        saved = json.loads(receipt.read_text())

        other = Path(self.temp.name) / "caller.json"
        project_bridge.Project.initialize(other, "other", self.root, OWNER, runtime="isolated")
        new = project_bridge.Project(other, runner=FakeHC())
        receipt.unlink()  # A run closed by the previous version has no receipt.
        self.assertTrue(new._slot_run_closed({"run": started["run"]}))
        project_bridge.Project.persist(receipt, {**saved, "run": ".artifacts/runs/wrong-run"})
        with self.assertRaises(BridgeError) as blocked:
            new.start("next", source_home=source)
        self.assertEqual(blocked.exception.code, "project_admission_unresolved")
        project_bridge.Project.persist(receipt, saved)

        private_json = project_bridge.private_json

        def forbid_old_run_records(path, **kwargs):
            if path.is_relative_to(run) and path != receipt:
                self.fail("admission must not read the previous run's large journal")
            return private_json(path, **kwargs)

        with patch.object(project_bridge, "private_json", side_effect=forbid_old_run_records):
            self.assertFalse(new.start("next", source_home=source)["closed"])
        self.addCleanup(new.close, "next")

    def test_abandon_start_releases_provably_unlaunched_request_without_replay(self):
        from test_pi_bridge import FakeHC

        self.init()
        project_bridge.Project.enable_shared_starts(self.config)
        source = self.source()
        hc = FakeHC()
        project = project_bridge.Project(self.config, runner=hc)
        collision = self.root / ".artifacts/runs/test-project-fixed"
        collision.mkdir(parents=True)
        with patch("secrets.token_hex", return_value="fixed"), self.assertRaises(FileExistsError):
            project.start("interrupted", source_home=source)

        def abandon(*, bound=False):
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                if bound:
                    with self.assertRaises(SystemExit) as rejected:
                        project_bridge.main(
                            ["abandon-start", "interrupted"], bound_config=self.config
                        )
                    self.assertEqual(rejected.exception.code, 2)
                else:
                    self.assertEqual(
                        project_bridge.main(
                            [
                                "--config",
                                str(self.config),
                                "abandon-start",
                                "interrupted",
                            ]
                        ),
                        0,
                        errors.getvalue(),
                    )
            return json.loads(output.getvalue()) if not bound else None

        abandon(bound=True)
        self.assertEqual(abandon()["delivery"], "not_attempted")
        self.assertEqual(abandon()["delivery"], "not_attempted")
        self.assertTrue(collision.is_dir())
        with self.assertRaises(BridgeError) as retried:
            project.start("interrupted", source_home=source)
        self.assertEqual(retried.exception.code, "project_start_abandoned")
        self.assertEqual(hc.count("run"), 0)
        self.assertFalse(project.start("next", source_home=source)["closed"])
        self.addCleanup(project.close, "next")
        self.assertEqual(hc.count("run"), 1)

    def test_abandoning_an_intent_without_a_slot_does_not_release_another_entry(self):
        from test_pi_bridge import FakeHC

        self.init()
        project_bridge.Project.enable_shared_starts(self.config)
        first_hc = FakeHC()
        first = project_bridge.Project(self.config, runner=first_hc)
        source = self.source()
        persist = project_bridge.Project.persist

        def interrupted(path, data):
            if path.name == "slot.json":
                raise OSError("shared write interrupted")
            persist(path, data)

        with (
            patch.object(project_bridge.Project, "persist", side_effect=interrupted),
            self.assertRaisesRegex(OSError, "shared write interrupted"),
        ):
            first.start("not-launched", source_home=source)
        self.assertEqual(first_hc.count("run"), 0)
        second_path = Path(self.temp.name) / "second.json"
        project_bridge.Project.initialize(
            second_path, "second", self.root, OWNER, runtime="isolated"
        )
        second = project_bridge.Project(second_path, runner=FakeHC())
        second.start("now-running", source_home=source)
        self.addCleanup(second.close, "now-running")

        self.assertTrue(first.abandon_start("not-launched")["abandoned"])
        with self.assertRaises(BridgeError) as blocked:
            first.start("must-wait", source_home=source)
        self.assertEqual(blocked.exception.code, "project_start_conflict")
        self.assertEqual(first_hc.count("run"), 0)

    def test_historical_run_is_read_in_place_without_start_or_prompt_replay(self):
        from test_pi_bridge import FakeHC

        from herdr_agent_bridge.runtime_paths import LEGACY_RUNS

        self.init()
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        started = entry.start("historical", source_home=self.source())
        # Construct an old-layout fixture; no real runtime directory is migrated.
        legacy = self.root / LEGACY_RUNS / "historical"
        legacy.parent.mkdir(parents=True)
        (self.root / started["run"]).rename(legacy)
        data = json.loads(self.config.read_text())
        data.pop("agent")
        data["starts"]["historical"]["request"].pop("agent")
        data["starts"]["historical"]["run"] = str(legacy.relative_to(self.root))
        project_bridge.save(self.config, data)
        manager_path = legacy / "manager.json"
        manager_data = json.loads(manager_path.read_text())
        manager_data["run"] = str(legacy.relative_to(self.root))
        project_bridge.save(manager_path, manager_data)
        before = {
            p: p.read_bytes() for p in (self.config, manager_path, legacy / "bridge/journal.json")
        }
        hc.calls.clear()
        recovered = project_bridge.Project(self.config, runner=hc)
        self.addCleanup(recovered.close, "historical")
        self.assertEqual(recovered.status()["run_id"], started["run_id"])
        self.assertEqual(recovered.start("historical")["run_id"], started["run_id"])
        self.assertEqual(hc.count("run"), 0)
        self.assertEqual(hc.count("send"), 0)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertTrue(recovered.close("historical")["closed"])
        self.assertTrue(legacy.is_dir())

    def test_agent_policy_refusals_leave_shared_start_unreserved(self):
        from test_pi_bridge import FakeHC

        self.init()
        project_bridge.Project.enable_shared_starts(self.config)
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        config_before = self.config.read_bytes()
        attempts = (
            {"agent": "unknown"},
            {"agent": "pi", "model": "gpt-6.2-sol"},
            {"agent": "codex", "thinking": "off"},
            {"agent": "codex", "development": {"task_id": "would-reserve"}},
        )
        for parameters in attempts:
            with self.subTest(parameters=parameters), self.assertRaises(BridgeError) as raised:
                entry.start("candidate", **parameters)
            self.assertEqual(raised.exception.code, "execution_policy_invalid")
        self.assertEqual(hc.calls, [])
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertFalse((self.root / ".artifacts/runs").exists())
        valid = entry.start("candidate", source_home=self.source())
        self.addCleanup(entry.close, "candidate")
        self.assertEqual(valid["agent"], "pi")

    def test_old_request_with_no_agent_keeps_pi_after_project_default_changes(self):
        from test_pi_bridge import FakeHC

        hc = FakeHC()
        self.init()
        entry = project_bridge.Project(self.config, runner=hc)
        original = entry.start("original", model="openai/original", source_home=self.source())
        self.addCleanup(entry.close, "original")
        data = json.loads(self.config.read_text())
        data.pop("agent")
        data["starts"]["original"]["request"].pop("agent")
        project_bridge.save(self.config, data)
        self.assertEqual(entry.configuration()["agent"], "pi")
        self.assertEqual(entry.defaults(agent="codex")["agent"], "codex")
        before = self.config.read_bytes()
        for agent in (None, "pi"):
            replayed = entry.start("original", agent=agent, model="openai/original")
            self.assertEqual(
                (replayed["run_id"], replayed["agent"], replayed["execution_policy"]),
                (original["run_id"], "pi", original["execution_policy"]),
            )
        calls = list(hc.calls)
        for overrides in (
            {"agent": "codex", "model": "openai/original"},
            {"model": "openai/different"},
            {"model": "openai/original", "thinking": "medium"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(BridgeError) as raised:
                entry.start("original", **overrides)
            self.assertEqual(raised.exception.code, "project_start_mismatch")
        self.assertEqual(hc.calls, calls)
        self.assertEqual(hc.count("run"), 1)
        self.assertEqual(self.config.read_bytes(), before)

    def test_known_codex_model_effort_is_rejected_before_reserving(self):
        from test_pi_bridge import FakeHC

        self.init(runtime="native")
        project_bridge.Project.enable_shared_starts(self.config)
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        before = self.config.read_bytes()
        (Path(self.temp.name) / "models_cache.json").write_text(
            json.dumps(
                {
                    "models": [
                        {"slug": "gpt-fixture", "supported_reasoning_levels": [{"effort": "high"}]}
                    ]
                }
            )
        )
        with (
            patch.dict(os.environ, {"CODEX_HOME": self.temp.name}),
            self.assertRaises(BridgeError) as raised,
        ):
            entry.start("candidate", agent="codex", model="gpt-fixture", thinking="low")
        self.assertEqual(raised.exception.code, "execution_policy_invalid")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(hc.calls, [])
        self.assertFalse((self.root / ".artifacts/runs").exists())

    def test_fixed_entry_dispatches_explicit_agent_model_and_reasoning(self):
        from test_pi_bridge import FakeHC

        self.init(runtime="native")
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        contract = Path(self.temp.name) / "contract.json"
        contract.write_text(
            json.dumps(
                {
                    "task_id": "repair-report",
                    "goal": "Update the report value",
                    "allowed_paths": ["code.py"],
                    "acceptance": ["Report value is updated"],
                    "checks": ["value"],
                }
            )
        )
        output = io.StringIO()
        with (
            patch.object(project_bridge, "Project", return_value=entry),
            patch.dict(os.environ, {"CODEX_HOME": self.temp.name}),
            redirect_stdout(output),
        ):
            code = project_bridge.main(
                [
                    "start",
                    "codex-request",
                    "--agent",
                    "codex",
                    "--model",
                    "gpt-6.2-sol",
                    "--reasoning",
                    "high",
                    "--development-contract",
                    str(contract),
                ],
                bound_config=self.config,
            )
        self.assertEqual(code, 0)
        self.addCleanup(entry.close, "codex-request")
        result = json.loads(output.getvalue())
        self.assertEqual(result["agent"], "codex")
        self.assertEqual(result["development"]["task_id"], "repair-report")
        self.assertEqual(
            result["execution_policy"]["resolved"], {"model": "gpt-6.2-sol", "thinking": "high"}
        )
        request = entry.record("codex-request")[1]
        self.assertEqual(request["request"]["agent"], "codex")
        bridge = entry.manager(request).bridge()
        hc.terminal["labels"] = {"bridge_run": bridge.data["session"]}
        self.assertEqual(bridge.agent(), "codex")
        launch = next(args for args, _, _ in hc.calls if args[0] == "run")
        self.assertIn("codex", launch)
        self.assertNotIn("pi", launch)
        self.assertIn("gpt-6.2-sol", launch)
        self.assertIn('model_reasoning_effort="high"', launch)
        self.assertEqual(hc.count("send"), 0)
        output = io.StringIO()
        with patch.object(project_bridge, "Project", return_value=entry), redirect_stdout(output):
            code = project_bridge.main(
                [
                    "native-command",
                    "unsupported",
                    "/name",
                    "--arguments",
                    "report",
                    "--request-id",
                    "codex-request",
                ],
                bound_config=self.config,
            )
        self.assertEqual(code, 0)
        operation = json.loads(output.getvalue())
        self.assertEqual(operation["run_id"], result["run_id"])
        self.assertEqual(operation["native_operation"]["state"], "unsupported")
        self.assertEqual(hc.count("send"), 0)

    def test_bound_cli_uses_its_project_config_and_rejects_config_overrides(self):
        self.init()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(project_bridge.main(["config"], bound_config=self.config), 0)
        self.assertEqual(json.loads(output.getvalue())["project_id"], "test-project")
        for option in ("--config", "--conf", "--con"):
            with self.subTest(option=option), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    project_bridge.main(
                        [option, str(self.root / "other.json"), "config"], bound_config=self.config
                    )
                self.assertEqual(raised.exception.code, 2)

    def test_cli_initializes_with_an_existing_app_space(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = project_bridge.main(
                [
                    "init",
                    "--project-id",
                    "test-project",
                    "--project",
                    str(self.root),
                    "--bot-id",
                    OWNER["bot_id"],
                    "--conversation-id",
                    OWNER["conversation_id"],
                    "--app-space",
                    "space-01",
                ],
                bound_config=self.config,
            )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["app_space"], "space-01")

    def test_changing_app_space_applies_only_to_future_starts(self):
        from test_pi_bridge import FakeHC

        self.init(app_space="space-01")
        entry = project_bridge.Project(self.config, runner=FakeHC())
        started = entry.start("original", source_home=self.source())
        self.addCleanup(entry.close, "original")
        original = json.loads(self.config.read_text())["starts"]["original"]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                project_bridge.main(["app-space", "space-02"], bound_config=self.config), 0
            )
        updated = json.loads(output.getvalue())
        self.assertEqual(updated["app_space"], "space-02")
        self.assertEqual(updated["applies_to"], "future_starts")
        self.assertFalse(updated["running_session_changed"])
        data = json.loads(self.config.read_text())
        self.assertEqual(data["starts"]["original"], original)
        self.assertEqual(entry.status()["run_id"], started["run_id"])
        for invalid in ("", " ", "space/other", "space=other", "line\nbreak", "x" * 201, None, 42):
            with self.subTest(invalid=invalid), self.assertRaises(BridgeError):
                entry.app_space(invalid)
        self.assertEqual(entry.configuration()["app_space"], "space-02")
        entry.close()
        entry.runtime("native")
        next_hc, _, app_command = self.native_app_fixture("next", "space-02")
        next_entry = project_bridge.Project(self.config, runner=next_hc)
        with patch("herdr_agent_bridge.app_session.subprocess.run", side_effect=app_command):
            future = next_entry.start("next")
        self.addCleanup(next_entry.close, "next")
        self.assertEqual(future["app"]["space"], "space-02")
        self.assertEqual(future["app"]["binding"], "bound")
        self.assertEqual(
            json.loads(self.config.read_text())["starts"]["next"]["app_space"], "space-02"
        )

    def native_app_fixture(self, request_id="native", space="space-01"):
        from test_pi_bridge import FakeHC

        hc = FakeHC()

        def run(args, env, timeout):
            label = args[args.index("--label") + 1]
            hc.terminal["labels"] = dict([label.split("=", 1)])
            return {"address": hc.terminal["address"]}

        def label(args, env, timeout):
            record = json.loads(self.config.read_text())["starts"][request_id]
            self.assertEqual(record["app_space"], space)
            app = json.loads((self.root / record["run"] / "app.json").read_text())
            self.assertEqual((app["binding"], app["space"]), ("unknown", space))
            key, value = args[2].split("=", 1)
            hc.terminal["labels"][key] = value
            return {}

        hc.handlers.update(run=run, label=label)
        app_commands = []

        def app_command(args, **kwargs):
            app_commands.append(args)
            self.assertEqual(args, ["herdr-app", "ls"])
            visible = hc.terminal["labels"].get("herdr.space") == space
            tabs = (
                [
                    {
                        "id": "owned-tab",
                        "terminals": [{"terminal": hc.terminal["address"], "pane": "owned-pane"}],
                    }
                ]
                if visible
                else []
            )
            mapping = {
                "spaces": [
                    {"id": space, "machine": None, "tabs": tabs},
                    {
                        "id": "unrelated-space",
                        "machine": None,
                        "tabs": [
                            {
                                "id": "unrelated-tab",
                                "terminals": [
                                    {"terminal": "unrelated-terminal", "pane": "unrelated-pane"}
                                ],
                            }
                        ],
                    },
                ]
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(mapping))

        return hc, app_commands, app_command

    def test_native_start_binds_the_owned_terminal_to_the_configured_app_space(self):
        self.init(runtime="native", app_space="space-01")
        hc, app_commands, app_command = self.native_app_fixture()
        entry = project_bridge.Project(self.config, runner=hc)
        with patch("herdr_agent_bridge.app_session.subprocess.run", side_effect=app_command):
            started = entry.start("native")
            repeated = entry.start("native")
        self.assertEqual(started["app"]["space"], "space-01")
        self.assertEqual(started["app"]["binding"], "bound")
        self.assertEqual(repeated["run_id"], started["run_id"])
        self.assertEqual(repeated["app"]["binding"], "bound")
        self.assertEqual(hc.count("run"), 1)
        self.assertEqual(hc.count("label"), 1)
        self.assertTrue(app_commands)
        self.assertNotIn("unrelated-terminal", json.dumps(started))

    def test_lost_app_binding_reply_keeps_the_original_run_and_is_not_replayed(self):
        self.init(runtime="native", app_space="space-01")
        hc, _, app_command = self.native_app_fixture()
        original_label = hc.handlers["label"]

        def lost_label(args, env, timeout):
            original_label(args, env, timeout)
            raise subprocess.TimeoutExpired("hc label", timeout)

        hc.handlers["label"] = lost_label
        entry = project_bridge.Project(self.config, runner=hc)
        with patch("herdr_agent_bridge.app_session.subprocess.run", side_effect=app_command):
            started = entry.start("native")
            entry.app_space("space-02")
            repeated = entry.start("native")
        self.assertEqual(repeated["run_id"], started["run_id"])
        for result in (started, repeated):
            self.assertEqual(result["app"]["space"], "space-01")
            self.assertEqual(result["app"]["binding"], "unknown")
            self.assertTrue(result["app"]["pending"])
            self.assertIn("error", result["app"])
        self.assertEqual(hc.count("run"), 1)
        self.assertEqual(hc.count("label"), 1)

    def test_app_unavailable_keeps_run_identity_and_reports_pending_in_status(self):
        self.init(runtime="native", app_space="space-01")
        hc, _, _ = self.native_app_fixture()
        entry = project_bridge.Project(self.config, runner=hc)
        with patch(
            "herdr_agent_bridge.app_session.subprocess.run", side_effect=FileNotFoundError()
        ) as app_command:
            started = entry.start("native")
            repeated = entry.start("native")
            status = entry.status()
        self.assertEqual(app_command.call_count, 1)
        self.assertEqual(hc.count("run"), 1)
        self.assertEqual(hc.count("label"), 0)
        for result in (started, repeated, status):
            self.assertEqual(result["run_id"], started["run_id"])
            self.assertEqual(result["app"]["binding"], "pending")
            self.assertTrue(result["app"]["pending"])
            self.assertIn("error", result["app"])

    def test_saved_app_mapping_cannot_report_an_unrelated_terminal_as_bound(self):
        self.init(runtime="native", app_space="space-01")
        hc, _, app_command = self.native_app_fixture()
        entry = project_bridge.Project(self.config, runner=hc)
        with patch("herdr_agent_bridge.app_session.subprocess.run", side_effect=app_command):
            started = entry.start("native")
        app_path = self.root / started["run"] / "app.json"
        saved = json.loads(app_path.read_text())
        saved["visible"][0]["terminal"] = "unrelated-terminal"
        app_path.write_text(json.dumps(saved))
        repeated = entry.start("native")
        self.assertEqual(repeated["run_id"], started["run_id"])
        self.assertTrue(repeated["app"]["pending"])
        self.assertIn("error", repeated["app"])
        self.assertNotIn("unrelated-terminal", json.dumps(repeated))
        self.assertEqual(hc.count("run"), 1)
        self.assertEqual(hc.count("label"), 1)

    def test_app_state_is_pending_if_its_bridge_evidence_cannot_be_verified(self):
        self.init(runtime="native", app_space="space-01")
        hc, _, app_command = self.native_app_fixture()
        entry = project_bridge.Project(self.config, runner=hc)
        with patch("herdr_agent_bridge.app_session.subprocess.run", side_effect=app_command):
            started = entry.start("native")
        journal = self.root / started["run"] / "bridge" / "journal.json"
        journal.chmod(0o644)
        result = entry.start("native")
        self.assertEqual(result["run_id"], started["run_id"])
        self.assertTrue(result["app"]["pending"])
        self.assertIn("error", result["app"])

    def test_monitor_uses_existing_run_and_refuses_duplicate_observer(self):
        from test_pi_bridge import FakeHC

        self.init()
        entry = project_bridge.Project(self.config, runner=FakeHC())
        started = entry.start("monitor", source_home=self.source())
        self.addCleanup(entry.close)
        launches = []

        def launcher(command, **kwargs):
            launches.append(command)
            return SimpleNamespace(pid=12345)

        monitored = entry.monitor(seconds=30, launcher=launcher)
        self.assertEqual(monitored["run_id"], started["run_id"])
        self.assertEqual(monitored["monitor"]["seconds"], 30)
        with self.assertRaises(BridgeError):
            entry.monitor(seconds=30, launcher=launcher)
        self.assertEqual(len(launches), 1)

    def native_callback_entry(self):
        from test_pi_bridge import FakeHC

        webhook = Path(self.temp.name) / "native-webhook.json"
        webhook.write_text(
            json.dumps(
                {**OWNER, "url": "https://example.invalid/hook", "key": "FIXTURE-NATIVE-KEY"}
            )
        )
        webhook.chmod(0o600)
        self.init(runtime="native", webhook_config=webhook)
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        started = entry.start("native-callback")
        root = self.root / started["run"] / "bridge"
        journal = json.loads((root / "journal.json").read_text())
        hc.terminal["labels"] = {"bridge_run": journal["session"]}
        (root / "pi-selection.json").write_text(
            json.dumps(
                {
                    "state": "ready",
                    **started["execution_policy"]["resolved"],
                    "session_id": "native-session",
                }
            )
        )
        return entry, hc, root

    def test_native_submission_automatically_starts_callback_observer(self):
        entry, hc, root = self.native_callback_entry()
        with patch.object(
            project_bridge.Manager,
            "monitor",
            return_value={"monitor": {"backend": "hc", "address": "owned-watcher"}},
        ) as monitor:
            result = entry.submit("review", "Inspect the authorized task")
        self.assertEqual(hc.count("send"), 1)
        monitor.assert_called_once_with()
        self.assertEqual(result["monitor"]["backend"], "hc")
        self.assertEqual(result["next_action"], "await_callback")
        self.assertEqual(
            json.loads((root / "journal.json").read_text())["tasks"][0]["delivery"], "submitted"
        )

    def test_native_monitor_timeout_preserves_submitted_task_without_replay(self):
        entry, hc, root = self.native_callback_entry()
        with patch.object(
            project_bridge.Manager,
            "monitor",
            side_effect=subprocess.TimeoutExpired(["hc", "run"], 20),
        ):
            result = entry.submit("review", "Inspect the authorized task")
        self.assertIn("monitor_error", result)
        self.assertEqual(result["next_action"], "inspect_monitor_without_resubmitting")
        self.assertEqual(hc.count("send"), 1)
        self.assertEqual(len(json.loads((root / "journal.json").read_text())["tasks"]), 1)
        with self.assertRaises(BridgeError):
            entry.submit("review", "Inspect the authorized task")
        self.assertEqual(hc.count("send"), 1)

    def test_public_output_redacts_binding_and_endpoint_in_values_and_object_keys(self):
        webhook = Path(self.temp.name) / "private-webhook.json"
        endpoint = "https://example.invalid/project-private-hook"
        credential = "PRIVATE-WEBHOOK-CREDENTIAL"
        webhook.write_text(json.dumps({**OWNER, "url": endpoint, "key": credential}))
        webhook.chmod(0o600)
        entry = self.init(webhook_config=webhook)
        secrets = [str(self.root), str(webhook), *OWNER.values(), endpoint, credential]
        value = {
            "result": [{item: "prefix " + item + " suffix"} for item in secrets],
            "owner": OWNER,
            "project_root": str(self.root),
            "webhook_config": str(webhook),
        }
        public = json.dumps(entry.public(value))
        for item in secrets:
            self.assertNotIn(item, public)

    def test_config_and_defaults_sanitize_private_binding_in_public_names(self):
        entry = project_bridge.Project.initialize(
            self.config,
            OWNER["bot_id"],
            self.root,
            OWNER,
            checks=[{**CHECKS[0], "name": OWNER["conversation_id"]}],
        )
        outputs = [entry.configuration(), entry.defaults(model="openai/" + OWNER["bot_id"])]
        for output in outputs:
            for value in OWNER.values():
                self.assertNotIn(value, json.dumps(output))

    def test_reloaded_webhook_credential_is_rejected_before_prompt_or_decision(self):
        from test_pi_bridge import FakeHC

        webhook = Path(self.temp.name) / "webhook.json"
        webhook.write_text(
            json.dumps({**OWNER, "url": "https://example.invalid/hook", "key": "OLD-KEY"})
        )
        webhook.chmod(0o600)
        self.init(webhook_config=webhook)
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        started = entry.start("rotation", source_home=self.source())
        self.addCleanup(entry.close)
        (self.root / started["run"] / "bridge" / "pi-selection.json").write_text(
            json.dumps(
                {
                    "state": "ready",
                    **started["execution_policy"]["resolved"],
                    "session_id": "fixture-session",
                }
            )
        )
        webhook.write_text(
            json.dumps({**OWNER, "url": "https://example.invalid/hook", "key": "NEW-KEY"})
        )
        with self.assertRaisesRegex(BridgeError, "credential material"):
            entry.submit("leaking-prompt", "This must not reach Pi: NEW-KEY")
        with self.assertRaisesRegex(BridgeError, "credential material"):
            entry.decide("terminate", "operator", "This must not be journaled: NEW-KEY")
        self.assertEqual(hc.count("send"), 0)

    def test_private_ids_cannot_rename_fixed_status_or_verification_fields(self):
        owner = {"bot_id": "status", "conversation_id": "passed"}
        entry = project_bridge.Project.initialize(self.config, "safe-project", self.root, owner)
        self.assertEqual(
            entry.public({"execution": {"status": "idle"}, "passed": False}),
            {"execution": {"status": "idle"}, "passed": False},
        )
        for field in (
            "model_selection",
            "actual",
            "native_evidence",
            "changed_paths",
            "outside_scope",
            "stable_during_checks",
            "worktree_sha256",
            "collected_at",
            "round_id",
            "native",
            "args",
            "reconciled",
            "exit_code",
            "output",
            "output_truncated",
            "control_result",
            "sessions",
        ):
            with self.subTest(field=field):
                bound = project_bridge.Project.initialize(
                    self.config.with_name(field + ".json"),
                    "safe-project",
                    self.root,
                    {"bot_id": field, "conversation_id": "private-conversation"},
                )
                self.assertEqual(bound.public({field: None}), {field: None})

    def test_private_config_recovers_and_public_view_omits_private_binding(self):
        self.init()
        recovered = project_bridge.Project(self.config)
        self.assertEqual(
            recovered.configuration()["defaults"],
            {"model": "openai/gpt-6.1-sol", "thinking": "high"},
        )
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        private = json.loads(self.config.read_text())
        self.assertEqual(private["project_root"], str(self.root.resolve()))
        self.assertEqual(private["owner"], OWNER)
        public = json.dumps(recovered.configuration())
        self.assertNotIn(str(self.root), public)
        self.assertNotIn(OWNER["bot_id"], public)
        with self.assertRaises(BridgeError):
            self.init()

    def test_repeated_start_recovers_original_run_without_creating_second_session(self):
        from test_pi_bridge import FakeHC

        self.init()
        source = self.root / ".artifacts" / "pi-source"
        source.mkdir()
        (source / "settings.json").write_text("{}")
        (source / "auth.json").write_text("{}")
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        first = entry.start("request-one", source_home=source)
        self.addCleanup(lambda: project_bridge.Project(self.config, runner=hc).close())
        repeated = project_bridge.Project(self.config, runner=hc).start(
            "request-one", source_home=source
        )
        self.assertEqual(repeated["run_id"], first["run_id"])
        self.assertEqual(hc.count("run"), 1)
        self.assertEqual(
            first["execution_policy"]["sources"],
            {"model": "project_default", "thinking": "project_default"},
        )
        with self.assertRaises(BridgeError):
            entry.start("request-two", source_home=source)

    def test_defaults_are_per_field_persistent_and_never_change_running_session(self):
        from test_pi_bridge import FakeHC

        self.init()
        source = self.root / ".artifacts" / "pi-source"
        source.mkdir()
        (source / "settings.json").write_text("{}")
        (source / "auth.json").write_text("{}")
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        first = entry.start("one", model="openai/custom-model", source_home=source)
        self.addCleanup(entry.close)
        updated = entry.defaults(thinking="medium")
        self.assertEqual(updated["applies_to"], "future_starts")
        self.assertFalse(updated["running_session_changed"])
        still_running = project_bridge.Project(self.config, runner=hc).status()
        self.assertEqual(
            still_running["execution_policy"]["resolved"],
            {"model": "openai/custom-model", "thinking": "high"},
        )
        entry.close()
        hc.terminal = {
            "address": "fixture-second",
            "state": "live",
            "turn": 0,
            "status": {"state": "idle"},
            "created_ms": 0,
        }
        second = entry.start("two", source_home=source)
        self.addCleanup(entry.close)
        self.assertEqual(
            second["execution_policy"]["resolved"],
            {"model": "openai/gpt-6.1-sol", "thinking": "medium"},
        )
        self.assertEqual(first["execution_policy"]["sources"]["model"], "task_override")

    def test_development_uses_named_checks_and_same_run_continuation_and_cleanup(self):
        from test_pi_bridge import FakeHC

        self.init()
        source = self.root / ".artifacts" / "pi-source"
        source.mkdir()
        (source / "settings.json").write_text("{}")
        (source / "auth.json").write_text("{}")
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        spec = {
            "task_id": "change-value",
            "goal": "Change the value",
            "allowed_paths": ["code.py"],
            "acceptance": ["Value equals two"],
            "checks": ["value"],
        }
        started = entry.start("dev", spec, source_home=source)
        self.addCleanup(entry.close)
        (self.root / started["run"] / "bridge" / "pi-selection.json").write_text(
            json.dumps(
                {
                    "state": "ready",
                    **started["execution_policy"]["resolved"],
                    "session_id": "fixture-session",
                }
            )
        )
        turn = entry.submit("round-one", "Inspect and report first")
        self.assertEqual(turn["run_id"], started["run_id"])
        self.assertEqual(turn["task"]["task_id"], "round-one")
        self.assertEqual(hc.count("send"), 1)
        with self.assertRaises(BridgeError) as stale:
            entry.verify("dev", task_id="older-round")
        self.assertEqual(stale.exception.code, "verification_round_not_current")
        entry.decide("terminate", "test-operator", "Fixture finished")
        self.assertTrue(entry.close()["closed"])
        self.assertEqual(entry.result()["run_id"], started["run_id"])
        with self.assertRaises(BridgeError):
            entry.submit("round-two", "Do not send")
        self.assertEqual(hc.count("send"), 1)

    def test_notification_checks_saved_event_then_refreshes_before_ack_and_never_submits(self):
        from test_pi_bridge import FakeHC

        self.webhook = Path(self.temp.name) / "private-webhook.json"
        self.webhook.write_text(
            json.dumps(
                {
                    **OWNER,
                    "url": "https://example.invalid/hook",
                    "key": "DO-NOT-EXPOSE-THIS-CREDENTIAL",
                }
            )
        )
        self.webhook.chmod(0o600)
        self.init(webhook_config=self.webhook)
        source = self.root / ".artifacts" / "pi-source"
        source.mkdir()
        (source / "settings.json").write_text("{}")
        (source / "auth.json").write_text("{}")
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc, sender=lambda *args: 200)
        started = entry.start("notification", source_home=source)
        self.addCleanup(entry.close)
        (self.root / started["run"] / "bridge" / "pi-selection.json").write_text(
            json.dumps(
                {
                    "state": "ready",
                    **started["execution_policy"]["resolved"],
                    "session_id": "fixture-session",
                }
            )
        )
        event_id = entry.status(observe=True)["event_id"]
        entry.notify(event_id)
        payload = json.loads((self.root / started["run"] / "notifications.json").read_text())[
            "events"
        ][event_id]["payload"]
        before = hc.count("get")
        handled = entry.notification(payload, receipt="grok-fixture-receipt")
        self.assertGreater(hc.count("get"), before)
        self.assertEqual(handled["notification"]["acknowledged"], True)
        self.assertEqual(hc.count("send"), 0)
        entry.close()
        closed_gets = hc.count("get")
        self.assertTrue(entry.notification(payload, receipt="grok-fixture-receipt")["closed"])
        self.assertEqual(hc.count("get"), closed_gets)
        for mutation in (
            {"run": "../escape"},
            {"run": "/absolute/escape"},
            {"run": ".artifacts/runs/unbound"},
            {"run_id": "wrong-run"},
            {"event_id": "unknown-event"},
            {"kind": "question"},
            {"owner": {**OWNER, "conversation_id": "other"}},
        ):
            with self.assertRaises(BridgeError):
                entry.notification({**payload, **mutation}, receipt="do-not-ack")
        self.assertNotIn("DO-NOT-EXPOSE-THIS-CREDENTIAL", json.dumps(handled))
        self.assertNotIn(str(self.root), json.dumps(handled))

    def test_intent_survives_failure_before_manager_creation_and_is_never_replayed(self):
        from test_pi_bridge import FakeHC

        self.init()
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        # A filesystem collision is a deterministic pre-creation failure.
        collision = self.root / ".artifacts/runs/test-project-fixed"
        collision.mkdir(parents=True)
        with patch("secrets.token_hex", return_value="fixed"):
            with self.assertRaises(FileExistsError):
                entry.start("uncertain", source_home=self.source())
        recovered = project_bridge.Project(self.config, runner=hc)
        self.assertEqual(recovered.configuration()["active_request"], "uncertain")
        for request_id in ("uncertain", "second-request"):
            with self.assertRaisesRegex(BridgeError, "creation unresolved"):
                recovered.start(request_id)
        self.assertEqual(hc.count("run"), 0)

    def test_lost_launch_reply_recovers_original_binding_without_second_hc_run(self):
        from test_pi_bridge import FakeHC

        self.init()
        project_bridge.Project.enable_shared_starts(self.config)
        hc = FakeHC()

        def timeout(args, env, duration):
            persisted = json.loads(self.config.read_text())
            self.assertEqual(persisted["active_request"], "uncertain")
            self.assertIsNone(persisted["starts"]["uncertain"]["run_id"])
            hc.terminal["labels"] = {"bridge_run": args[args.index("--label") + 1].split("=", 1)[1]}
            raise subprocess.TimeoutExpired("fixture hc", duration)

        hc.handlers["run"] = timeout
        entry = project_bridge.Project(self.config, runner=hc)
        self.addCleanup(entry.close)
        with self.assertRaises(subprocess.TimeoutExpired):
            entry.start("uncertain", source_home=self.source())
        recovered = project_bridge.Project(self.config, runner=hc)
        with self.assertRaisesRegex(BridgeError, "creation evidence"):
            recovered.abandon_start("uncertain")
        status = recovered.start("uncertain")
        self.assertEqual(status["launch"], "delivery_unknown")
        self.assertIsNotNone(status["run_id"])
        self.assertEqual(hc.count("run"), 1)
        with self.assertRaises(BridgeError):
            recovered.start("different")

    def test_bound_manager_owner_and_run_and_symlink_paths_are_checked_before_io(self):
        from test_pi_bridge import FakeHC

        self.init()
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        started = entry.start("original", source_home=self.source())
        self.addCleanup(entry.close)
        manager_path = self.root / started["run"] / "manager.json"
        valid = manager_path.read_text()
        for field, value in (
            ("owner", {**OWNER, "bot_id": "other"}),
            ("run_id", "wrong-run"),
            ("project", str(self.root.parent)),
        ):
            with self.subTest(field=field):
                data = json.loads(valid)
                data[field] = value
                manager_path.write_text(json.dumps(data))
                try:
                    with self.assertRaises(BridgeError):
                        entry.status(observe=True)
                finally:
                    manager_path.write_text(valid)
        backup = manager_path.with_suffix(".backup")
        manager_path.rename(backup)
        manager_path.symlink_to(backup)
        try:
            with self.assertRaises(BridgeError):
                entry.status(observe=True)
        finally:
            manager_path.unlink()
            backup.rename(manager_path)
        self.assertEqual(hc.count("get"), 0)

    def test_cli_init_defaults_and_safe_error_output(self):
        script = SCRIPTS / "project_bridge.py"
        checks = Path(self.temp.name) / "checks.json"
        checks.write_text(json.dumps(CHECKS))
        command = [sys.executable, str(script), "--config", str(self.config)]
        init = subprocess.run(
            command
            + [
                "init",
                "--project-id",
                "cli-project",
                "--project",
                str(self.root),
                "--bot-id",
                OWNER["bot_id"],
                "--conversation-id",
                OWNER["conversation_id"],
                "--checks-file",
                str(checks),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(json.loads(init.stdout)["project_id"], "cli-project")
        updated = subprocess.run(
            command + ["defaults", "--thinking", "medium"], capture_output=True, text=True
        )
        self.assertEqual(json.loads(updated.stdout)["defaults"]["thinking"], "medium")
        bad = subprocess.run(command + ["status"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("error", json.loads(bad.stderr))
        self.assertNotIn(str(self.root), init.stdout + updated.stdout + bad.stderr)

    def cli_hc_fixture(self):
        source = self.source()
        fixture_bin = Path(self.temp.name) / "bin"
        fixture_bin.mkdir()
        log = Path(self.temp.name) / "hc-calls.jsonl"
        hc = fixture_bin / "hc"
        hc.write_text(
            f"#!{sys.executable}\n"
            + """import json, os, sys
with open(os.environ['PROJECT_TEST_HC_LOG'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
command = sys.argv[1]
result = {'run': {'address': 'fixture-terminal'},
          'get': {'address': 'fixture-terminal', 'state': 'live', 'turn': 0,
                  'status': {'state': 'idle'}, 'created_ms': 0},
          'send': {'started': True}, 'ls': {'terminals': []}}.get(command, {})
print(json.dumps(result))
"""
        )
        hc.chmod(0o700)
        env = {
            **os.environ,
            "PATH": str(fixture_bin) + os.pathsep + os.environ.get("PATH", ""),
            "PI_CODING_AGENT_DIR": str(source),
            "PROJECT_TEST_HC_LOG": str(log),
        }
        return env, log

    def test_concurrent_bound_and_direct_starts_create_only_one_run(self):
        self.init()
        second = Path(self.temp.name) / "second.json"
        project_bridge.Project.initialize(second, "second", self.root, OWNER, runtime="isolated")
        project_bridge.Project.enable_shared_starts(self.config, second)
        env, log = self.cli_hc_fixture()
        bound = (
            "import sys; from herdr_agent_bridge.project_entry import run_bound; "
            "sys.stdin.readline(); sys.exit(run_bound(sys.argv[1], sys.argv[2:]))"
        )
        direct = (
            "import sys; from herdr_agent_bridge.project_bridge import main; "
            "sys.stdin.readline(); sys.exit(main(sys.argv[1:]))"
        )
        commands = [
            [sys.executable, "-c", bound, str(self.config)],
            [sys.executable, "-c", direct, "--config", str(second)],
        ]
        with (
            subprocess.Popen(
                commands[0] + ["start", "one"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            ) as first,
            subprocess.Popen(
                commands[1] + ["start", "two"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            ) as other,
        ):
            for process in (first, other):
                process.stdin.write("go\n")
                process.stdin.flush()
            replies = []
            for process, command in zip((first, other), commands, strict=True):
                stdout, stderr = process.communicate(timeout=15)
                if process.returncode == 0:
                    self.addCleanup(
                        subprocess.run,
                        command + ["close"],
                        input="go\n",
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=15,
                    )
                replies.append((process.returncode, stdout, stderr))
        self.assertEqual(sorted(code for code, _, _ in replies), [0, 1], replies)
        error = json.loads(next(stderr for code, _, stderr in replies if code == 1))
        self.assertEqual(error["code"], "project_start_conflict")
        self.assertEqual(error["delivery"], "not_attempted")
        self.assertNotIn(str(self.root), json.dumps(error))
        self.assertEqual(
            sum(json.loads(line)[0] == "run" for line in log.read_text().splitlines()), 1
        )
        self.assertEqual(len(list((self.root / ".artifacts/runs").glob("*/manager.json"))), 1)

    def test_cli_lifecycle_uses_one_bound_run_with_disposable_hc_executable(self):
        self.init()
        env, log = self.cli_hc_fixture()
        command = [sys.executable, str(SCRIPTS / "project_bridge.py"), "--config", str(self.config)]

        def cli(*args, success=True):
            result = subprocess.run(command + list(args), capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn(str(self.root), result.stdout + result.stderr)
            return json.loads(result.stdout if success else result.stderr)

        started = cli("start", "cli-request")
        self.addCleanup(
            subprocess.run, command + ["close"], capture_output=True, text=True, env=env
        )
        self.assertEqual(cli("start", "cli-request")["run_id"], started["run_id"])
        cli("start", "other", success=False)
        selection = self.root / started["run"] / "bridge" / "pi-selection.json"
        selection.write_text(
            json.dumps(
                {
                    "state": "ready",
                    **started["execution_policy"]["resolved"],
                    "session_id": "fixture-session",
                }
            )
        )
        self.assertIsNotNone(cli("status", "--observe")["event_id"])
        prompt = Path(self.temp.name) / "prompt.txt"
        prompt.write_text("Inspect the supplied fixture")
        self.assertEqual(
            cli("submit", "round-one", "--prompt-file", str(prompt))["task"]["task_id"], "round-one"
        )
        self.assertEqual(cli("result")["task"]["task_id"], "round-one")
        cli("monitor", "--seconds", "0", success=False)
        self.assertTrue(cli("close")["closed"])
        self.assertTrue(cli("result")["closed"])
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(sum(args[0] == "run" for args in calls), 1)
        self.assertEqual(sum(args[0] == "send" for args in calls), 1)

    def test_cli_rejects_oversized_deep_duplicate_and_non_json_callbacks_safely(self):
        self.init()
        payload = Path(self.temp.name) / "payload.json"
        command = [
            sys.executable,
            str(SCRIPTS / "project_bridge.py"),
            "--config",
            str(self.config),
            "notification",
            "--payload-file",
            str(payload),
        ]
        for raw in (
            b" " * 65_537,
            b"[" * 2000 + b"]" * 2000,
            b'{"event_id":"one","event_id":"two"}',
            b'{"value":NaN}',
            b"\xff",
        ):
            with self.subTest(raw=raw[:40]):
                payload.write_bytes(raw)
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stderr)["error"], "project_operation_failed")
                self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(
            project_bridge.Project(self.config).configuration()["active_request"], None
        )

    def test_explicit_invalid_initial_policy_or_checks_are_not_replaced_by_defaults(self):
        for options in ({"defaults": {}}, {"checks": {}}, {"checks": False}):
            with self.subTest(options=options):
                with self.assertRaises(BridgeError):
                    project_bridge.Project.initialize(
                        self.config, "invalid", self.root, OWNER, **options
                    )
                self.assertFalse(self.config.exists())

    def test_webhook_reference_drift_fails_before_using_another_bound_endpoint(self):
        from test_pi_bridge import FakeHC

        webhooks = []
        for name in ("first", "second"):
            path = Path(self.temp.name) / (name + ".json")
            path.write_text(
                json.dumps(
                    {**OWNER, "url": "https://example.invalid/" + name, "key": name + "-KEY"}
                )
            )
            path.chmod(0o600)
            webhooks.append(path)
        self.init(webhook_config=webhooks[0])
        hc = FakeHC()
        entry = project_bridge.Project(self.config, runner=hc)
        entry.start("original", source_home=self.source())
        self.addCleanup(entry.close)
        original = self.config.read_text()
        data = json.loads(original)
        data["webhook_config"] = str(webhooks[1])
        self.config.write_text(json.dumps(data))
        try:
            with self.assertRaisesRegex(BridgeError, "notification config changed"):
                entry.status(observe=True)
            self.assertEqual(hc.count("get"), 0)
        finally:
            self.config.write_text(original)

    def test_webhook_credential_cannot_collide_with_fixed_protocol_fields(self):
        webhook = Path(self.temp.name) / "webhook.json"
        webhook.write_text(
            json.dumps({**OWNER, "url": "https://example.invalid/hook", "key": "status"})
        )
        webhook.chmod(0o600)
        with self.assertRaises(BridgeError):
            self.init(webhook_config=webhook)
        self.assertFalse(self.config.exists())

    def test_public_output_fails_closed_if_redaction_placeholder_contains_credential(self):
        webhook = Path(self.temp.name) / "webhook.json"
        webhook.write_text(
            json.dumps({**OWNER, "url": "https://example.invalid/hook", "key": "<private>"})
        )
        webhook.chmod(0o600)
        entry = self.init(webhook_config=webhook)
        with self.assertRaises(BridgeError):
            entry.public({"result": str(self.root)})

    def test_error_codes_are_filtered_when_they_collide_with_a_known_credential(self):
        key = "execution_policy_invalid"
        webhook = Path(self.temp.name) / "webhook.json"
        webhook.write_text(json.dumps({**OWNER, "url": "https://example.invalid/hook", "key": key}))
        webhook.chmod(0o600)
        self.init(webhook_config=webhook)
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = project_bridge.main(["defaults", "--model", "invalid"], bound_config=self.config)
        self.assertEqual(code, 1)
        self.assertNotIn(key, error.getvalue())
        self.assertNotEqual(json.loads(error.getvalue())["code"], key)

    def test_private_config_rejects_shared_permissions_symlinks_and_deep_json(self):
        self.init()
        original = self.config.read_bytes()
        self.config.chmod(0o644)
        with self.assertRaises(BridgeError):
            project_bridge.Project(self.config)
        self.config.chmod(0o600)
        backup = self.config.with_suffix(".backup")
        self.config.rename(backup)
        self.config.symlink_to(backup)
        with self.assertRaises(BridgeError):
            project_bridge.Project(self.config)
        self.config.unlink()
        backup.rename(self.config)
        self.config.write_bytes(b"[" * 2000 + b"]" * 2000)
        with self.assertRaises(BridgeError):
            project_bridge.Project(self.config)
        self.config.write_bytes(original)

    def test_malformed_config_and_unsafe_bindings_fail_without_tracebacks(self):
        self.init()
        data = json.loads(self.config.read_text())
        data["starts"] = {
            "bad": {"run": ".artifacts/runs/bad", "run_id": None, "request": {}, "policy": {}}
        }
        self.config.write_text(json.dumps(data))
        with self.assertRaises(BridgeError):
            project_bridge.Project(self.config)

    def test_rejects_malformed_persisted_request_policy_and_active_binding(self):
        from test_pi_bridge import FakeHC

        self.init()
        entry = project_bridge.Project(self.config, runner=FakeHC())
        entry.start("original", source_home=self.source())
        self.addCleanup(entry.close)
        valid = self.config.read_text()
        mutations = [
            ("active_request", []),
            ("run_id", []),
            ("request", []),
            ("request", {"development": None, "model": None}),
            ("policy", {"requested": {}, "resolved": dict(project_bridge.DEFAULTS), "sources": {}}),
            (
                "policy",
                {
                    "requested": {"model": None, "thinking": None},
                    "resolved": dict(project_bridge.DEFAULTS),
                    "sources": {"model": "task_override", "thinking": "project_default"},
                },
            ),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                data = json.loads(valid)
                if key == "active_request":
                    data[key] = value
                else:
                    data["starts"]["original"][key] = value
                self.config.write_text(json.dumps(data))
                try:
                    with self.assertRaises(BridgeError):
                        project_bridge.Project(self.config)
                finally:
                    self.config.write_text(valid)
