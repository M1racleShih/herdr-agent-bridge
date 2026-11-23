import importlib.util
import json
import sys
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

from herdr_agent_bridge.bridge_common import save

PROFILE = "fixture-hermes"
SESSION = "fixture-original-chat"
ACTOR = "fixture-local-user"
CHAT = "local-fixture-route"


class Viewer:
    profile_name = PROFILE
    answer = "register"

    def __init__(self, on_question):
        self.questions = []
        self.on_question = on_question

    def register_command(self, name, handler, **kwargs):
        assert name == "hab-register"
        self.command = handler

    def ask_choice(self, question, options):
        self.questions.append((question, options))
        self.on_question(question)
        return self.answer


class HermesRegistrationTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.base = Path(t.name).resolve()
        self.project = self.base / "work" / "report"
        (self.project / ".git").mkdir(parents=True)
        self.catalog = self.base / "catalog.json"
        self.binding = self.base / "binding.json"
        self.binding_body = {
            "format": 1,
            "owner": {"bot_id": PROFILE, "conversation_id": SESSION},
            "registry": str(self.catalog),
            "allowed_roots": [str(self.project.parent)],
            "projects": {},
        }
        save(self.binding, self.binding_body)
        plugin = self.base / "plugin"
        plugin.mkdir()
        with (plugin / "__init__.py").open("w") as copy:
            copy.write(
                files("herdr_agent_bridge.resources.hermes_project_registration")
                .joinpath("__init__.py")
                .read_text()
            )
        self.settings = plugin / "settings.json"
        save(
            self.settings,
            {
                "format": 1,
                "profile": PROFILE,
                "session_id": SESSION,
                "chat_id": CHAT,
                "user_id": ACTOR,
                "binding": str(self.binding),
                "bridge_python": str(Path(sys.executable).absolute()),
            },
        )
        spec = importlib.util.spec_from_file_location(
            "fixture_registration", plugin / "__init__.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.viewer = Viewer(self.waiting)
        self.module.register(self.viewer)

    def waiting(self, question):
        self.assertIn(str(self.project), question)
        self.assertFalse(self.catalog.exists())
        self.assertFalse((self.project / ".artifacts").exists())

    def invoke(self, query="report", *, session=SESSION, user=ACTOR, platform="local", chat=CHAT):
        source = SimpleNamespace(user_id=user, chat_id=chat, chat_type="dm")
        return self.viewer.command(query, session_id=session, platform=platform, source=source)

    def test_confirmation_displays_exact_directory_and_creates_identity_only(self):
        result = self.invoke()
        [entry] = json.loads(self.catalog.read_text())["projects"]
        self.assertEqual(entry["root"], str(self.project))
        self.assertIn(entry["project_id"], result)
        self.assertEqual(json.loads(self.binding.read_text()), self.binding_body)
        self.assertFalse((self.project / ".artifacts").exists())
        self.assertIn(entry["project_id"], self.invoke(str(self.project)))
        self.assertEqual(len(self.viewer.questions), 1)

    def test_wrong_session_user_platform_or_chat_cannot_open_the_confirmation(self):
        for args in (
            {"session": "other"},
            {"user": "other"},
            {"platform": "webhook"},
            {"chat": "other"},
        ):
            self.assertIn("not authorized", self.invoke(**args))
        self.assertFalse(self.viewer.questions)
        self.assertFalse(self.catalog.exists())

    def test_decline_or_no_answer_does_not_register(self):
        for answer in ("cancel", None):
            self.viewer.answer = answer
            self.assertIn("Not registered", self.invoke())
        self.assertFalse(self.catalog.exists())

    def test_expired_confirmation_does_not_register(self):
        self.module.CONFIRM_SECONDS = 0
        self.assertIn("Not registered", self.invoke())
        self.assertEqual(len(self.viewer.questions), 1)
        self.assertFalse(self.catalog.exists())

    def test_changed_scope_or_directory_cannot_reuse_the_confirmation(self):
        def revoke(question):
            self.waiting(question)
            save(self.binding, {**self.binding_body, "allowed_roots": []})

        self.viewer.on_question = revoke
        self.assertIn("Not registered", self.invoke())
        self.assertFalse(self.catalog.exists())
        save(self.binding, self.binding_body)

        def replace(question):
            self.waiting(question)
            self.project.rename(self.project.with_name("old"))
            (self.project / ".git").mkdir(parents=True)

        self.viewer.on_question = replace
        self.assertIn("Not registered", self.invoke())
        self.assertFalse(self.catalog.exists())

    def test_same_name_requires_a_path_before_showing_the_confirmation(self):
        (self.project.parent / "other" / "report" / ".git").mkdir(parents=True)
        result = self.invoke()
        self.assertIn("Choose an exact directory", result)
        self.assertIn(str(self.project), result)
        self.assertEqual(self.viewer.questions, [])
        self.assertFalse(self.catalog.exists())
        self.assertIn("Registered", self.invoke(str(self.project)))

    def test_confirmation_does_not_survive_a_catalog_retarget(self):
        other = self.base / "another-catalog.json"

        def retarget(question):
            self.waiting(question)
            save(self.binding, {**self.binding_body, "registry": str(other)})

        self.viewer.on_question = retarget
        self.assertIn("Not registered", self.invoke())
        self.assertFalse(self.catalog.exists())
        self.assertFalse(other.exists())
