"""Launch and agent-home contracts using only disposable credential fixtures."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_agent_bridge import agent_adapters
from herdr_agent_bridge.bridge_common import BridgeError
from herdr_agent_bridge.resource_paths import resource_path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class AgentAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "run"
        self.root.mkdir()
        self.scratch = self.base / "scratch"
        self.scratch.mkdir()
        self.source = self.base / "source"
        self.source.mkdir()
        self.project = self.base / "project"
        self.project.mkdir()
        self.data = {
            "scratch": str(self.scratch),
            "project": str(self.project),
            "model": "fixture-model",
        }

    def test_codex_legacy_and_explicit_launch_preserve_sandbox_and_daemon_flags(self):
        expected = [
            "codex",
            "--no-daemon",
            "--no-alt-screen",
            "-s",
            "read-only",
            "-a",
            "on-request",
            "-m",
            "fixture-model",
        ]
        self.assertEqual(agent_adapters.build_agent_command(self.data, self.root), expected)

        self.data["agent"] = "codex"
        self.assertEqual(agent_adapters.build_agent_command(self.data, self.root), expected)

    def test_native_pi_restores_saved_environment_and_preserves_normal_resources(self):
        self.data.update(
            agent="pi",
            runtime="native",
            model="provider/model/with/slash",
            thinking="high",
            native_env={
                "HOME": "/fixture/home",
                "PATH": "/fixture/bin",
                "HC_SESSION": "normal-core",
                "PI_CODING_AGENT_DIR": "/fixture/pi",
                "CODEX_HOME": None,
            },
        )
        expected = ["env"]
        for key in (*agent_adapters.PARENT_AGENT_KEYS, *agent_adapters.NATIVE_ENV_KEYS):
            if key in agent_adapters.PARENT_AGENT_KEYS or self.data["native_env"].get(key) is None:
                expected.extend(["-u", key])
        expected.extend(
            [
                "HOME=/fixture/home",
                "PATH=/fixture/bin",
                "HC_SESSION=normal-core",
                "PI_CODING_AGENT_DIR=/fixture/pi",
                "PI_PROGRAM_STATUS=1",
                "HAB_PI_SELECTION_FILE=" + str(self.root / "pi-selection.json"),
                "HAB_PI_RESOURCES_FILE=" + str(self.root / "pi-resources.json"),
                "HAB_PI_SETTLED_FILE=" + str(self.root / "pi-settled.json"),
                "HAB_PI_MODEL=provider/model/with/slash",
                "HAB_PI_THINKING=high",
                "pi",
                "--session-dir",
                str(self.scratch / "pi" / "sessions"),
                "-e",
                str(resource_path("pi_native_observer.mjs")),
                "--provider",
                "provider",
                "--model",
                "model/with/slash",
                "--thinking",
                "high",
            ]
        )
        with patch.dict(os.environ, {"HOME": "/wrong", "PI_CODING_AGENT_DIR": "/wrong"}):
            self.assertEqual(agent_adapters.build_agent_command(self.data, self.root), expected)
        self.data.update(model=None, thinking=None)
        command = agent_adapters.build_agent_command(self.data, self.root)
        self.assertEqual(
            command[-5:],
            [
                "pi",
                "--session-dir",
                str(self.scratch / "pi" / "sessions"),
                "-e",
                str(resource_path("pi_native_observer.mjs")),
            ],
        )
        self.assertIn("HAB_PI_MODEL=", command)
        self.assertIn("HAB_PI_THINKING=", command)

    def test_isolated_pi_cli_has_exact_readonly_flags(self):
        self.data.update(agent="pi", model="provider/model")
        self.assertEqual(
            agent_adapters.build_agent_command(self.data, self.root),
            [
                "pi",
                "--tui-mode",
                "regular",
                "--no-approve",
                "--no-extensions",
                "--no-mcp",
                "--no-skills",
                "--no-prompt-templates",
                "--no-themes",
                "--tools",
                "read,grep,find,ls",
                "--session-dir",
                str(self.scratch / "pi" / "sessions"),
                "--model",
                "provider/model",
            ],
        )

    def test_isolated_pi_sdk_keeps_auth_development_and_thinking_selection_flags(self):
        self.data.update(agent="pi", model="provider/model")
        for flags in (
            {"pi_auth_mode": "native_shared"},
            {"development": {"contract": {}}},
            {"thinking": "off"},
            {"pi_auth_mode": "native_shared", "development": {"contract": {}}, "thinking": "high"},
        ):
            for executable in ("/fixture/bin/pi", None):
                with self.subTest(flags=flags, executable=executable):
                    data = {**self.data, **flags}
                    expected = [
                        "node",
                        str(resource_path("pi_oauth_session.mjs")),
                        executable or "pi",
                        str(self.scratch / "pi"),
                        str(self.project),
                        "provider/model",
                    ]
                    if flags.get("development"):
                        expected.append("development")
                    if flags.get("thinking") is not None:
                        expected.extend(
                            [
                                "--thinking",
                                flags["thinking"],
                                "--selection-file",
                                str(self.root / "pi-selection.json"),
                            ]
                        )
                    with patch(
                        "herdr_agent_bridge.agent_adapters.shutil.which", return_value=executable
                    ):
                        self.assertEqual(
                            agent_adapters.build_agent_command(data, self.root), expected
                        )

    def test_prepare_isolated_codex_copies_fixture_auth_and_exact_project_trust_only(self):
        (self.source / "config.toml").write_text(
            'model_provider = "fixture"\nservice_tier = "fast"\n'
            'mcp_servers = { private = { command = "forbidden" } }\n'
            '[model_providers.fixture]\nname = "fixture"\nbase_url = "https://fixture.invalid"\n'
            "[projects." + json.dumps(str(self.project)) + ']\ntrust_level = "trusted"\n'
            '[projects."/unrelated"]\ntrust_level = "trusted"\n'
        )
        (self.source / "auth.json").write_text('{"fixture":"DISPOSABLE"}')
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        agent_adapters.prepare_isolated_agent(self.data, self.root, self.source)
        import tomllib

        destination = self.scratch / "codex"
        config = tomllib.loads((destination / "config.toml").read_text())
        self.assertEqual(
            config,
            {
                "model_provider": "fixture",
                "service_tier": "fast",
                "model_providers": {
                    "fixture": {"name": "fixture", "base_url": "https://fixture.invalid"}
                },
                "projects": {str(self.project): {"trust_level": "trusted"}},
                "model": "fixture-model",
                "model_reasoning_effort": "max",
                "sandbox_mode": "read-only",
                "approval_policy": "on-request",
            },
        )
        self.assertEqual((destination / "auth.json").read_bytes(), before["auth.json"])
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        for path in destination.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()}, before)

    def test_prepare_isolated_pi_selects_only_api_key_provider_and_saves_model(self):
        self.data.update(agent="pi", model=None)
        (self.source / "settings.json").write_text(
            json.dumps(
                {
                    "defaultProvider": "fixture",
                    "defaultModel": "model/with/slash",
                    "defaultThinkingLevel": "low",
                    "skills": ["do-not-copy"],
                    "httpProxy": "http://fixture.invalid",
                    "transport": "sse",
                }
            )
        )
        (self.source / "auth.json").write_text(
            json.dumps(
                {
                    "fixture": {"type": "api_key", "key": "DISPOSABLE-SELECTED"},
                    "unselected": {"type": "api_key", "key": "DISPOSABLE-OTHER"},
                }
            )
        )
        (self.source / "models.json").write_text("""\ufeff{
            // Comment before provider configuration.
            "providers": {"fixture": {"baseUrl":"https://fixture.invalid/v1", "models":[]},
                          "unselected": {"apiKey":"DISPOSABLE-OTHER"}} /* comment */}""")
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        agent_adapters.prepare_isolated_agent(self.data, self.root, self.source)
        home = self.scratch / "pi"
        self.assertEqual(self.data["model"], "fixture/model/with/slash")
        self.assertEqual(self.data["pi_auth_mode"], "isolated_api_key")
        self.assertEqual(json.loads((self.root / "journal.json").read_text()), self.data)
        self.assertEqual(
            json.loads((home / "auth.json").read_text()),
            {"fixture": {"type": "api_key", "key": "DISPOSABLE-SELECTED"}},
        )
        self.assertEqual(
            json.loads((home / "models.json").read_text()),
            {"providers": {"fixture": {"baseUrl": "https://fixture.invalid/v1", "models": []}}},
        )
        settings = json.loads((home / "settings.json").read_text())
        self.assertNotIn("skills", settings)
        self.assertEqual(settings["defaultThinkingLevel"], "low")
        self.assertEqual(settings["httpProxy"], "http://fixture.invalid")
        self.assertEqual(settings["retry"]["provider"]["maxRetries"], 0)
        self.assertFalse(settings["compaction"]["enabled"])
        self.assertFalse((home / "auth-source.json").exists())
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        for path in home.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()}, before)

    def test_prepare_pi_oauth_shares_fixture_store_and_keeps_development_policy_and_thinking(self):
        self.data.update(
            agent="pi",
            model="fixture/model",
            thinking="high",
            development={
                "contract": {"allowed_paths": ["result.txt"], "checks": {"fixture": ["true"]}}
            },
        )
        (self.source / "auth.json").write_text(
            json.dumps(
                {
                    "fixture": {
                        "type": "oauth",
                        "access": "DISPOSABLE-ACCESS",
                        "refresh": "DISPOSABLE-REFRESH",
                        "expires": 123456789,
                    }
                }
            )
        )
        before = (self.source / "auth.json").read_bytes()
        agent_adapters.prepare_isolated_agent(self.data, self.root, self.source)
        home = self.scratch / "pi"
        self.assertEqual(self.data["pi_auth_mode"], "native_shared")
        self.assertFalse((home / "auth.json").exists())
        self.assertEqual(
            json.loads((home / "auth-source.json").read_text()),
            {"path": str((self.source / "auth.json").resolve())},
        )
        self.assertEqual(
            json.loads((home / "settings.json").read_text())["defaultThinkingLevel"], "high"
        )
        self.assertEqual(
            json.loads((home / "development-policy.json").read_text()),
            {
                "project": str(self.project),
                "allowed_paths": ["result.txt"],
                "checks": {"fixture": ["true"]},
                "journal": str(self.root / "journal.json"),
            },
        )
        self.assertEqual((self.source / "auth.json").read_bytes(), before)

    def test_native_home_setup_leaves_normal_resources_untouched(self):
        self.data.update(agent="pi", runtime="native", model=None)
        fixture = self.source / "settings.json"
        fixture.write_text('{"skills":["fixture-skill"],"extensions":["fixture-extension"]}')
        before = fixture.read_bytes()
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.source)}):
            agent_adapters.prepare_isolated_agent(self.data, self.root)
        self.assertEqual(fixture.read_bytes(), before)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_codex_evidence_requires_exact_user_final_answer_and_ordered_completion(self):
        home = self.scratch / "codex"
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        path = sessions / "fixture.jsonl"
        task = {"id": "first", "submitted_text": "Exact fixture prompt"}

        def message(role, text, phase=None):
            return {
                "type": "response_item",
                "payload": {
                    "role": role,
                    "phase": phase,
                    "content": [{"text": text}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                },
            }

        events = [
            {"type": "session_meta", "payload": {"id": "thread-1"}},
            message("user", task["submitted_text"]),
            message("assistant", "commentary is not a result", "commentary"),
            message("assistant", "fixture result", "final_answer"),
        ]

        def write(records, suffix=""):
            path.write_text("".join(json.dumps(event) + "\n" for event in records) + suffix)

        expected = {
            "thread_id": "thread-1",
            "agent_turn_id": "turn-1",
            "answer": "fixture result",
            "agent_completed": False,
        }
        write(events)
        self.assertEqual(agent_adapters.extract_turns(home, [task]), {"first": expected})
        complete = {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}}
        write([*events, complete], '{"partial":')
        self.assertEqual(
            agent_adapters.extract_turns(home, [task]),
            {"first": {**expected, "agent_completed": True}},
        )
        write([*events, message("user", task["submitted_text"]), complete])
        self.assertEqual(agent_adapters.extract_turns(home, [task]), {})
        write(events, "invalid middle line\n{}\n")
        self.assertEqual(agent_adapters.extract_turns(home, [task]), {})
        write([*events, complete])
        self.assertEqual(
            agent_adapters.extract_turns(home, [{**task, "submitted_text": "Different"}]), {}
        )
        (sessions / "second.jsonl").write_text("")
        self.assertEqual(agent_adapters.extract_turns(home, [task]), {})

    def test_home_selection_uses_explicit_source_then_environment_then_fixture_home(self):
        for agent, selector, relative in (
            ("codex", "CODEX_HOME", ".codex"),
            ("pi", "PI_CODING_AGENT_DIR", ".pi/agent"),
        ):
            for selection in ("explicit", "environment", "default"):
                with self.subTest(agent=agent, selection=selection):
                    base = self.base / (agent + "-" + selection)
                    base.mkdir()
                    scratch = base / "scratch"
                    scratch.mkdir()
                    source = base / "selected"
                    if selection == "default":
                        source = base / relative
                    source.mkdir(parents=True)
                    (source / "auth.json").write_text(
                        '{"fixture":{"type":"api_key","key":"DISPOSABLE"}}'
                    )
                    data = {
                        **self.data,
                        "agent": agent,
                        "scratch": str(scratch),
                        "model": "fixture/model",
                    }
                    env = {"HOME": str(base)}
                    if selection != "default":
                        env[selector] = str(
                            source if selection == "environment" else base / "missing"
                        )
                    with patch.dict(os.environ, env, clear=True):
                        agent_adapters.prepare_isolated_agent(
                            data,
                            base / "run",
                            source_home=source if selection == "explicit" else None,
                        )
                    self.assertEqual(
                        json.loads((scratch / agent / "auth.json").read_text()),
                        {"fixture": {"type": "api_key", "key": "DISPOSABLE"}},
                    )

    def test_pi_rejects_shell_credentials_and_malformed_oauth_without_source_mutation(self):
        cases = (
            ({"fixture": {"type": "api_key", "key": " !DISPOSABLE-COMMAND"}}, None),
            (
                {
                    "fixture": {
                        "type": "oauth",
                        "access": "DISPOSABLE",
                        "refresh": "DISPOSABLE",
                        "expires": True,
                    }
                },
                None,
            ),
            (
                {
                    "fixture": {
                        "type": "oauth",
                        "access": "DISPOSABLE",
                        "refresh": "DISPOSABLE",
                        "expires": 123456789,
                    },
                    "other": {"type": "api_key", "key": "!DISPOSABLE-COMMAND"},
                },
                None,
            ),
            (
                {"fixture": {"type": "api_key", "key": "DISPOSABLE"}},
                {"providers": {"fixture": {"headers": {"Authorization": "!DISPOSABLE-COMMAND"}}}},
            ),
        )
        for index, (auth, models) in enumerate(cases):
            with self.subTest(case=index):
                (self.source / "auth.json").write_text(json.dumps(auth))
                if models:
                    (self.source / "models.json").write_text(json.dumps(models))
                before = {p.name: p.read_bytes() for p in self.source.iterdir()}
                with self.assertRaises(BridgeError) as caught:
                    agent_adapters.prepare_pi_home(
                        self.scratch / str(index), self.source, "fixture/model"
                    )
                self.assertNotIn("DISPOSABLE", str(caught.exception))
                self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
