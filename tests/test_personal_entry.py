"""Installed and bound multi-project CLI contracts; fixtures only."""

import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from herdr_agent_bridge import cli, personal_entry
from herdr_agent_bridge.bridge_common import BridgeError, save
from herdr_agent_bridge.project_bridge import Project
from herdr_agent_bridge.project_registry import ProjectRegistry

OWNER = {"bot_id": "personal-fixture", "conversation_id": "one-conversation"}


class PersonalEntryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.work = self.base / "work"
        self.work.mkdir()
        self.catalog = self.base / "catalog.json"
        self.binding = self.base / "caller.json"
        self.setup = {
            "format": 1,
            "registry": str(self.catalog),
            "allowed_roots": [str(self.work)],
            "owner": OWNER,
            "projects": {},
        }
        save(self.binding, self.setup)
        self.registry = ProjectRegistry(self.catalog, allowed_roots=[self.work])

    def call(self, *args, operator=False, **options):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            if operator:
                code = cli.main(["personal", "--binding", str(self.binding), *args])
            else:
                code = personal_entry.run_bound(self.binding, args, **options)
        return code, json.loads(output.getvalue() or error.getvalue())

    def test_bound_discovery_cannot_register_and_local_confirmation_is_reused(self):
        project = self.work / "report"
        (project / ".git").mkdir(parents=True)
        code, result = self.call("resolve", "report")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual([x["root"] for x in result["matches"]], [str(project)])
        self.assertFalse(self.catalog.exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as denied:
            self.call("register", str(project))
        self.assertEqual(denied.exception.code, 2)
        self.assertFalse(self.catalog.exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as denied:
            self.call("--binding", str(self.base / "other.json"), "discover")
        self.assertEqual(denied.exception.code, 2)
        code, registered = self.call("register", str(project), operator=True)
        self.assertEqual(code, 0)
        self.assertEqual(
            self.call("resolve", registered["project_id"]),
            (
                0,
                {"status": "registered", "matches": [registered]},
            ),
        )
        self.assertFalse((project / ".artifacts").exists())

    def provision(self, name, *, shared=True, owner=OWNER, runtime="isolated"):
        root = self.work / name
        (root / ".git").mkdir(parents=True)
        identifier = self.registry.register_confirmed(root)["project_id"]
        config = self.base / f"{name}.json"
        Project.initialize(config, f"legacy-{name}", root, owner, runtime=runtime)
        if shared:
            Project.enable_shared_starts(config)
        self.setup["projects"][identifier] = str(config)
        save(self.binding, self.setup)
        return identifier, root, config

    def pi_source(self):
        source = self.base / "pi-source"
        source.mkdir(exist_ok=True)
        (source / "settings.json").write_text("{}")
        (source / "auth.json").write_text("{}")
        return source

    def test_dispatch_is_scoped_to_selected_registered_project_and_rechecks_revocation(self):
        from test_pi_bridge import FakeHC

        first, first_root, first_config = self.provision("first")
        second, second_root, second_config = self.provision("second")
        hc_first, hc_second = FakeHC(), FakeHC()
        source = self.pi_source()
        launches = []
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(source)}):
            for project, config, hc in (
                (first, first_config, hc_first),
                (second, second_config, hc_second),
            ):
                args = ("--project-id", project, "--request-id", "same-request")
                code, started = self.call("start", *args, runner=hc)
                self.assertEqual(code, 0, started)
                self.addCleanup(Project(config, runner=hc).close, "same-request")
                self.assertEqual(started["project_id"], project)
                self.assertEqual(started["request_id"], "same-request")
                self.assertEqual(
                    self.call("result", *args, runner=hc)[1]["run_id"], started["run_id"]
                )
                self.assertEqual(hc.count("run"), 1)
                launches.append(started["run_id"])
        self.assertNotEqual(*launches)

        self.setup["allowed_roots"] = [str(first_root)]
        save(self.binding, self.setup)
        args = ("--project-id", second, "--request-id", "same-request")
        before = list(hc_second.calls)
        for operation, suffix in (
            ("start", ()),
            ("result", ()),
            ("submit", ("--round-id", "round-one")),
            ("native-command", ("native-one", "/name", "--arguments", "report")),
        ):
            code, failure = self.call(
                operation, *args, *suffix, runner=hc_second, input_stream=io.BytesIO(b"text")
            )
            self.assertEqual(code, 1)
            self.assertEqual(failure["code"], "project_not_authorized")
            self.assertEqual(failure["delivery"], "not_attempted")
            self.assertNotIn(str(second_root), json.dumps(failure))
        self.assertEqual(hc_second.calls, before)

    def test_registration_does_not_grant_execution_or_bypass_shared_gate_or_owner(self):
        from test_pi_bridge import FakeHC

        hc = FakeHC()
        known, _, config = self.provision("gated", shared=False)
        args = ("--project-id", known, "--request-id", "candidate")
        code, failure = self.call("start", *args, runner=hc)
        self.assertEqual(
            (code, failure["code"], failure["delivery"]),
            (1, "shared_start_required", "not_attempted"),
        )
        self.assertIsNone(Project(config).configuration()["active_request"])
        Project.enable_shared_starts(config)
        self.setup["owner"] = {**OWNER, "conversation_id": "another-conversation"}
        save(self.binding, self.setup)
        code, failure = self.call("start", *args, runner=hc)
        self.assertEqual(
            (code, failure["code"], failure["delivery"]),
            (1, "project_binding_mismatch", "not_attempted"),
        )

        self.setup["projects"] = {}
        save(self.binding, self.setup)
        code, failure = self.call("start", *args, runner=hc)
        self.assertEqual(
            (code, failure["code"], failure["delivery"]), (1, "project_not_bound", "not_attempted")
        )
        self.assertEqual(hc.calls, [])

    def test_same_binding_conflict_is_a_definite_refusal_before_backend_call(self):
        from test_pi_bridge import FakeHC

        hc = FakeHC()
        project_id, _, config = self.provision("same-binding")
        args = ("--project-id", project_id)
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.pi_source())}):
            code, started = self.call("start", *args, "--request-id", "active", runner=hc)
        self.assertEqual(code, 0, started)
        self.addCleanup(Project(config, runner=hc).close, "active")
        calls = list(hc.calls)
        code, conflict = self.call("start", *args, "--request-id", "conflict", runner=hc)
        self.assertEqual(
            (code, conflict["code"], conflict["delivery"]),
            (1, "project_start_conflict", "not_attempted"),
        )
        self.assertEqual(hc.calls, calls)
        self.assertEqual(Project(config).configuration()["active_request"], "active")

    def test_personal_start_and_fixed_replay_keep_the_same_codex_run(self):
        from test_pi_bridge import FakeHC

        hc = FakeHC()
        project_id, _, config = self.provision("selected", runtime="native")
        fixed = Project(config, runner=hc)
        fixed.defaults(agent="codex", model="gpt-6.2-sol", thinking="high")
        args = ("--project-id", project_id, "--request-id", "same")
        with patch.dict(os.environ, {"CODEX_HOME": str(self.base)}):
            code, started = self.call("start", *args, "--reasoning", "medium", runner=hc)
        self.assertEqual(code, 0, started)
        fixed = Project(config, runner=hc)
        self.addCleanup(fixed.close, "same")
        bridge = fixed.manager(fixed.record("same")[1]).bridge()
        hc.terminal["labels"] = {"bridge_run": bridge.data["session"]}
        self.assertEqual(started["agent"], "codex")
        self.assertEqual(
            started["execution_policy"]["resolved"], {"model": "gpt-6.2-sol", "thinking": "medium"}
        )
        fixed.defaults(agent="pi")
        self.assertEqual(fixed.start("same", thinking="medium")["run_id"], started["run_id"])
        self.assertEqual(
            self.call("start", *args, "--agent", "codex", "--thinking", "medium", runner=hc)[1][
                "run_id"
            ],
            started["run_id"],
        )
        before = list(hc.calls)
        code, conflict = self.call(
            "start", *args, "--agent", "pi", "--reasoning", "medium", runner=hc
        )
        self.assertEqual(
            (code, conflict["code"], conflict["delivery"]),
            (1, "project_start_mismatch", "not_attempted"),
        )
        self.assertEqual(hc.calls, before)
        self.assertEqual(hc.count("run"), 1)

    def test_fixed_pi_and_personal_codex_contend_for_one_project_slot(self):
        from test_pi_bridge import FakeHC

        project_id, root, personal_config = self.provision("race", shared=False, runtime="native")
        fixed_config = self.base / "fixed-race.json"
        Project.initialize(fixed_config, "fixed-race", root, OWNER, runtime="native")
        Project.enable_shared_starts(fixed_config, personal_config)
        hc_fixed, hc_personal = FakeHC(), FakeHC()
        fixed = Project(fixed_config, runner=hc_fixed)
        begin = Barrier(2, timeout=5)

        def fixed_start():
            begin.wait()
            try:
                started = fixed.start("fixed", agent="pi")
            except BridgeError as exc:
                return exc.code
            self.addCleanup(fixed.close, "fixed")
            bridge = fixed.manager(fixed.record("fixed")[1]).bridge()
            hc_fixed.terminal["labels"] = {"bridge_run": bridge.data["session"]}
            return started["agent"]

        with (
            patch.dict(os.environ, {"CODEX_HOME": str(self.base)}),
            ThreadPoolExecutor() as workers,
        ):
            fixed_outcome = workers.submit(fixed_start)
            begin.wait()
            code, personal = self.call(
                "start",
                "--project-id",
                project_id,
                "--request-id",
                "personal",
                "--agent",
                "codex",
                runner=hc_personal,
            )
            if code == 0:
                personal_project = Project(personal_config, runner=hc_personal)
                self.addCleanup(personal_project.close, "personal")
                bridge = personal_project.manager(personal_project.record("personal")[1]).bridge()
                hc_personal.terminal["labels"] = {"bridge_run": bridge.data["session"]}
                self.assertEqual(personal["agent"], "codex")
                self.assertEqual(fixed_outcome.result(), "project_start_conflict")
            else:
                self.assertEqual(personal["code"], "project_start_conflict")
                self.assertEqual(fixed_outcome.result(), "pi")
        self.assertEqual(hc_fixed.count("run") + hc_personal.count("run"), 1)

    def test_personal_native_command_is_separate_from_a_development_round(self):
        from test_pi_bridge import FakeHC

        hc = FakeHC()
        project_id, _, config = self.provision("native-command")
        fixed = Project(config, runner=hc)
        started = fixed.start("original", source_home=self.pi_source())
        self.addCleanup(fixed.close, "original")
        code, result = self.call(
            "native-command",
            "name-control",
            "/name",
            "--arguments",
            "fixture name",
            "--project-id",
            project_id,
            "--request-id",
            "original",
            runner=hc,
        )
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run_id"], started["run_id"])
        self.assertEqual(result["native_operation"]["state"], "unsupported")
        self.assertIsNone(result["task"])
        self.assertEqual(hc.count("send"), 0)

    def test_another_caller_cannot_read_original_request_or_create_a_second_run(self):
        from test_pi_bridge import FakeHC

        project_id, root, first_config = self.provision("shared", shared=False)
        first_hc, second_hc = FakeHC(), FakeHC()
        first = Project(first_config, runner=first_hc)
        original = first.start("owner-private-request", source_home=self.pi_source())
        self.addCleanup(first.close, "owner-private-request")
        second_owner = {"bot_id": "another-bot", "conversation_id": "another-conversation"}
        second_config = self.base / "second-entry.json"
        Project.initialize(second_config, "second-entry", root, second_owner, runtime="isolated")
        Project.enable_shared_starts(first_config, second_config)
        save(
            self.binding,
            {**self.setup, "owner": second_owner, "projects": {project_id: str(second_config)}},
        )
        code, failure = self.call(
            "result",
            "--project-id",
            project_id,
            "--request-id",
            "owner-private-request",
            runner=second_hc,
        )
        self.assertEqual((code, failure["delivery"]), (1, "not_attempted"))
        code, conflict = self.call(
            "start",
            "--project-id",
            project_id,
            "--request-id",
            "new-request",
            "--agent",
            "codex",
            runner=second_hc,
        )
        self.assertEqual(
            (code, conflict["code"], conflict["delivery"]),
            (1, "project_start_conflict", "not_attempted"),
        )
        for private in (original["run_id"], "owner-private-request", str(root), OWNER["bot_id"]):
            self.assertNotIn(private, json.dumps([failure, conflict]))
        self.assertEqual(second_hc.calls, [])
        self.assertEqual(first_hc.count("run"), 1)

    def test_legacy_run_can_be_read_and_continued_after_adoption(self):
        from test_pi_bridge import STAMP, FakeHC, assistant_message, session_header, user_message

        identifier, root, config = self.provision("legacy", shared=False)
        hc = FakeHC()
        old = Project(config, runner=hc)
        original = old.start("original", source_home=self.pi_source())
        self.addCleanup(old.close, "original")
        Project.enable_shared_starts(config)
        args = ("--project-id", identifier, "--request-id", "original")
        code, old_result = self.call("result", *args, runner=hc)
        self.assertEqual((code, old_result["run_id"]), (0, original["run_id"]))
        manager = old.manager(old.record("original")[1])
        bridge = manager.bridge()
        selected = original["execution_policy"]["resolved"]
        (bridge.root / "pi-selection.json").write_text(
            json.dumps(
                {
                    "state": "ready",
                    **selected,
                    "session_id": "pi-session-01",
                }
            )
        )
        code, first = self.call(
            "submit",
            *args,
            "--round-id",
            "first",
            runner=hc,
            input_stream=io.BytesIO("检查当前报告".encode()),
        )
        self.assertEqual(code, 0, first)
        self.assertEqual(first["round_id"], "first")
        submitted = manager.bridge().task("first")["submitted_text"]
        provider, model = selected["model"].split("/", 1)
        history = [
            session_header(root),
            {
                "type": "model_change",
                "id": "m",
                "parentId": None,
                "timestamp": STAMP,
                "provider": provider,
                "modelId": model,
            },
            {
                "type": "thinking_level_change",
                "id": "t",
                "parentId": "m",
                "timestamp": STAMP,
                "thinkingLevel": selected["thinking"],
            },
            user_message("u", "t", submitted),
            assistant_message(
                "a", "u", "first", {"answer": "fixture answer"}, provider=provider, model=model
            ),
        ]
        session = Path(bridge.data["scratch"]) / "pi/sessions/fixture.jsonl"
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text("".join(json.dumps(item) + "\n" for item in history))
        hc.terminal.update(turn=1, status={"state": "done"})
        code, observed = self.call("status", *args, "--round-id", "first", "--observe", runner=hc)
        self.assertEqual(code, 0, observed)
        self.assertEqual(observed["task"]["observed"], "evidence_ready", observed)
        status, verification = self.call(
            "verify", *args, "--round-id", "first", "--contract", "analysis", runner=hc
        )
        self.assertEqual(
            (status, verification["run_id"], verification["passed"]), (1, original["run_id"], False)
        )
        self.assertEqual(
            self.call("result", *args, "--round-id", "first", runner=hc)[1]["round_id"], "first"
        )
        code, second = self.call(
            "submit",
            *args,
            "--round-id",
            "second",
            "--parent-round-id",
            "first",
            runner=hc,
            input_stream=io.BytesIO(b"Follow up"),
        )
        self.assertEqual(code, 0, second)
        self.assertEqual(
            (second["run_id"], second["round_id"], second["task"]["parent"]),
            (original["run_id"], "second", "first"),
        )
        self.assertEqual((hc.count("run"), hc.count("send")), (1, 2))
        code, closed = self.call("close", *args, runner=hc)
        self.assertEqual(code, 0, closed)
        self.assertTrue(closed["closed"])

    def test_stdin_is_bounded_and_project_scope_is_loaded_after_input(self):
        from test_pi_bridge import FakeHC

        identifier, _, config = self.provision("interruptible")
        hc = FakeHC()
        args = ("--project-id", identifier, "--request-id", "request")
        invalid = (b"x" * (personal_entry.MAX_BODY + 1), b'{"goal":"one","goal":"two"}')
        for body in invalid:
            code, failure = self.call(
                "start", *args, "--development", runner=hc, input_stream=io.BytesIO(body)
            )
            self.assertEqual(
                (code, failure["code"], failure["delivery"]),
                (1, "invalid_task_input", "not_attempted"),
            )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as blocked:
            self.call("submit", *args, "--round-id", "first", "--prompt-file", str(self.binding))
        self.assertEqual(blocked.exception.code, 2)

        def revoke_and_read(_):
            save(self.binding, {**self.setup, "allowed_roots": []})
            return b"valid-body"

        payload = io.BytesIO(b"valid-body")
        with patch.object(payload, "read", side_effect=revoke_and_read):
            code, failure = self.call(
                "submit", *args, "--round-id", "first", runner=hc, input_stream=payload
            )
        self.assertEqual(
            (code, failure["code"], failure["delivery"]),
            (1, "project_not_authorized", "not_attempted"),
        )
        self.assertIsNone(Project(config).configuration()["active_request"])
        self.assertEqual(hc.calls, [])
