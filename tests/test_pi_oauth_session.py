"""Exercise the OAuth SDK launcher with a disposable synthetic Pi package."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_agent_bridge.resource_paths import resource_path

SCRIPT = resource_path("pi_oauth_session.mjs")
NODE = shutil.which("node")
SECRET = "fixture-secret-must-not-appear"


FAKE_UNDICI = r"""
const { appendFileSync } = require('node:fs');
const { EventEmitter } = require('node:events');
const mode = process.env.FAKE_PI_MODE || '';
const log = (event, value = {}) => appendFileSync(process.env.FAKE_PI_LOG,
    JSON.stringify({event, ...value}) + '\n');
const fail = () => { throw new Error('fixture-secret-must-not-appear'); };

class EnvHttpProxyAgent extends EventEmitter {
    constructor(options) {
        super();
        if (mode === 'network_error') fail();
        log('network_agent', {options, env: {
            HTTP_PROXY: process.env.HTTP_PROXY ?? null,
            HTTPS_PROXY: process.env.HTTPS_PROXY ?? null,
            http_proxy: process.env.http_proxy ?? null,
            https_proxy: process.env.https_proxy ?? null,
            no_proxy: process.env.no_proxy ?? null,
        }});
    }
    async destroy() {
        log('network_destroy');
        if (mode === 'network_destroy_error') fail();
    }
}
class Client extends EventEmitter {
    constructor(origin, options) {
        super();
        log('network_client', {origin, options});
    }
}
class Pool extends EventEmitter {
    constructor(origin, options) {
        super();
        log('network_pool', {origin, options});
    }
}
function setGlobalDispatcher() {
    log('network_set_dispatcher');
    if (mode === 'network_register_error') fail();
}
function install() {
    log('network_install');
    if (mode === 'network_install_error') fail();
}
module.exports = {EnvHttpProxyAgent, Client, Pool, setGlobalDispatcher, install};
"""


FAKE_SDK = r"""
import { appendFileSync } from 'node:fs';
import { join } from 'node:path';

const mode = process.env.FAKE_PI_MODE || '';
const log = (event, value = {}) => appendFileSync(process.env.FAKE_PI_LOG,
    JSON.stringify({event, ...value}) + '\n');
const fail = () => { throw new Error('fixture-secret-must-not-appear'); };
let themed = false;

export const ModelRuntime = {
    async create(options) {
        log('model_runtime', {options});
        if (mode === 'model_runtime_error') fail();
        return {
            getModel(provider, id) {
                log('get_model', {provider, id});
                if (mode === 'missing_model') return undefined;
                return {provider: mode === 'wrong_model' ? 'wrong' : provider, id};
            },
        };
    },
};
export const SettingsManager = {
    create(cwd, agentDir, options) {
        log('settings', {cwd, agentDir, options});
        return {getDefaultThinkingLevel: () => mode === 'bad_level' ? 'invalid' : 'high'};
    },
};
export const SessionManager = {
    create(cwd, directory) {
        log('session_manager', {cwd, directory});
        return {
            getCwd: () => cwd,
            getSessionDir: () => directory,
            getSessionFile: () => join(directory, 'fixture.jsonl'),
            getSessionId: () => 'fixture-session-id',
        };
    },
};
export async function createAgentSessionServices(options) {
    log('services', {cwd: options.cwd, agentDir: options.agentDir,
        resourceLoaderOptions: options.resourceLoaderOptions,
        modelRuntimeProvided: !!options.modelRuntime,
        settingsManagerProvided: !!options.settingsManager});
    return {cwd: options.cwd, agentDir: options.agentDir,
        diagnostics: mode === 'diagnostic_error' ? [{type: 'error', message: 'fixture-secret-must-not-appear'}] : []};
}
export async function createAgentSessionFromServices(options) {
    log('session', {model: options.model, tools: options.tools,
        thinkingLevel: options.thinkingLevel, sessionStartEvent: options.sessionStartEvent ?? null});
    return {session: {fixture: true, model: options.model, thinkingLevel: options.thinkingLevel,
        getAvailableThinkingLevels: () => mode === 'unsupported_level'
            ? ['off'] : ['off', 'low', 'medium', 'high']}};
}
export async function createAgentSessionRuntime(factory, options) {
    log('runtime', {cwd: options.cwd, agentDir: options.agentDir});
    if (mode === 'session_runtime_error') fail();
    const result = await factory(options);
    log('factory_result', {session: !!result.session, services: !!result.services,
        diagnostics: result.diagnostics});
    return {
        async recreate() {
            let changed = {};
            if (mode === 'changed_cwd') changed = {cwd: '/fixture-other-project'};
            if (mode === 'changed_agent') changed = {agentDir: '/fixture-other-agent'};
            if (mode === 'changed_session_dir') changed = {sessionManager: {
                ...options.sessionManager, getSessionDir: () => '/fixture-other-sessions',
            }};
            if (mode === 'changed_session_file') changed = {sessionManager: {
                ...options.sessionManager, getSessionFile: () => '/fixture-other-session.jsonl',
            }};
            const next = await factory({...options, ...changed,
                sessionStartEvent: {type: 'session_start', reason: 'new'}});
            log('recreated', {session: !!next.session, services: !!next.services,
                diagnostics: next.diagnostics});
        },
        async dispose() {
            log('dispose');
            if (mode === 'dispose_error') fail();
        },
    };
}
export function initTheme(name, watcher) {
    log('theme', {name: name ?? null, watcher});
    if (mode === 'theme_error') fail();
    themed = true;
}
export class InteractiveMode {
    constructor(runtime, options) {
        if (!themed) fail();
        if (mode === 'interactive_error') fail();
        this.runtime = runtime;
        log('interactive', {options});
    }
    async run() {
        log('run', {cwd: process.cwd(), argv: process.argv,
            aiAgent: process.env.AI_AGENT, piAgent: process.env.PI_CODING_AGENT,
            piDir: process.env.PI_CODING_AGENT_DIR,
            sessionDir: process.env.PI_CODING_AGENT_SESSION_DIR,
            offline: process.env.PI_OFFLINE, programStatus: process.env.PI_PROGRAM_STATUS});
        if (mode === 'run_error') fail();
        await this.runtime.recreate();
    }
}
"""


@unittest.skipUnless(NODE, "node is required for the Pi OAuth launcher")
class PiOAuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-pi-oauth-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.package = self.base / "node_modules" / "@fixture" / "pi"
        (self.package / "dist" / "bin").mkdir(parents=True)
        self.cli = self.package / "dist" / "bin" / "pi.mjs"
        self.cli.write_text("// synthetic CLI; not executed\n")
        self.link = self.base / "pi"
        self.link.symlink_to(self.cli)
        (self.package / "sdk.mjs").write_text(FAKE_SDK)
        (self.package / "wrong.mjs").write_text(
            "throw new Error('fixture-secret-must-not-appear');\n"
        )
        undici = self.package / "node_modules" / "undici"
        undici.mkdir(parents=True)
        (undici / "package.json").write_text(json.dumps({"name": "undici", "main": "index.cjs"}))
        (undici / "index.cjs").write_text(FAKE_UNDICI)
        self.manifest(
            name="@earendil-works/pi-coding-agent",
            exported={"import": "./sdk.mjs", "default": "./wrong.mjs"},
        )
        self.home = self.base / "scratch" / "pi"
        self.home.mkdir(parents=True)
        self.project = self.base / "project"
        self.project.mkdir()
        self.auth = self.base / "native" / "auth.json"
        self.auth.parent.mkdir()
        # Deliberately opaque fake bytes: the launcher must not read or copy them.
        self.native_bytes = b"not real credentials; preserve unrelated fixture providers\n"
        self.auth.write_bytes(self.native_bytes)
        self.profile({"path": str(self.auth)})
        (self.home / "models.json").write_text("{}\n")
        (self.home / "settings.json").write_text("{}\n")
        self.log_path = self.base / "events.jsonl"

    def manifest(self, name, exported):
        (self.package / "package.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "type": "module",
                    "exports": {".": exported},
                }
            )
        )

    def profile(self, profile):
        (self.home / "auth-source.json").write_text(json.dumps(profile))

    def launch(
        self,
        mode="",
        model="fixture-provider/model-with/slash",
        cli=None,
        env_overrides=None,
        thinking=None,
    ):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("NODE_", "PI_"))
            and key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        }
        env.update(FAKE_PI_LOG=str(self.log_path), FAKE_PI_MODE=mode)
        if env_overrides:
            env.update(env_overrides)
        selection = (
            ["--thinking", thinking, "--selection-file", str(self.base / "selection.json")]
            if thinking is not None
            else []
        )
        return subprocess.run(
            [
                NODE,
                str(SCRIPT),
                str(cli or self.link),
                str(self.home),
                str(self.project),
                model,
                *selection,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )

    def all_events(self):
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def events(self, kind):
        return [item for item in self.all_events() if item["event"] == kind]

    def assert_generic_failure(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "Pi OAuth session could not start or finish.\n")
        self.assertNotIn(SECRET, result.stderr)
        self.assertNotIn(str(self.auth), result.stderr)

    def test_native_auth_path_is_used_with_isolated_services_and_recreated_runtime(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        runtime = self.events("model_runtime")
        self.assertEqual(len(runtime), 1)
        self.assertEqual(
            runtime[0]["options"],
            {
                "authPath": str(self.auth),
                "modelsPath": str(self.home / "models.json"),
                "modelsStorePath": str(self.home / "models-store.json"),
                "allowModelNetwork": False,
            },
        )
        self.assertEqual(self.auth.read_bytes(), self.native_bytes)
        self.assertFalse((self.home / "auth.json").exists())
        self.assertEqual(
            self.events("session_manager")[0]["directory"], str(self.home / "sessions")
        )
        self.assertEqual(len(self.events("settings")), 2)
        for event in self.events("settings"):
            self.assertEqual((event["cwd"], event["agentDir"]), (str(self.project), str(self.home)))
            self.assertEqual(event["options"], {"projectTrusted": False})
        self.assertEqual(len(self.events("services")), 2)
        for event in self.events("services"):
            self.assertEqual(
                event["resourceLoaderOptions"],
                {
                    "noExtensions": True,
                    "disabledBuiltinExtensions": ["mcp"],
                    "noSkills": True,
                    "noPromptTemplates": True,
                    "noThemes": True,
                },
            )
            self.assertTrue(event["modelRuntimeProvided"])
            self.assertTrue(event["settingsManagerProvided"])
        sessions = self.events("session")
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            self.assertEqual(
                session["model"], {"provider": "fixture-provider", "id": "model-with/slash"}
            )
            self.assertEqual(session["tools"], ["read", "grep", "find", "ls"])
            self.assertEqual(session["thinkingLevel"], "high")
        self.assertEqual(sessions[1]["sessionStartEvent"]["reason"], "new")
        self.assertEqual(len(self.events("recreated")), 1)
        self.assertEqual(
            self.events("theme")[0], {"event": "theme", "name": None, "watcher": False}
        )
        self.assertEqual(self.events("interactive")[0]["options"], {"tuiMode": "regular"})
        self.assertEqual(len(self.events("dispose")), 1)
        for event in (
            "network_agent",
            "network_set_dispatcher",
            "network_install",
            "network_destroy",
        ):
            self.assertEqual(len(self.events(event)), 1)
        names = [item["event"] for item in self.all_events()]
        self.assertLess(names.index("network_agent"), names.index("network_set_dispatcher"))
        self.assertLess(names.index("network_set_dispatcher"), names.index("model_runtime"))
        self.assertLess(names.index("network_install"), names.index("model_runtime"))
        self.assertLess(names.index("dispose"), names.index("network_destroy"))
        run = self.events("run")[0]
        self.assertEqual(run["cwd"], str(self.project))
        self.assertNotIn(str(self.auth), run["argv"])
        self.assertEqual((run["aiAgent"], run["piAgent"]), ("pi", "true"))
        self.assertEqual(
            (run["piDir"], run["sessionDir"]), (str(self.home), str(self.home / "sessions"))
        )
        self.assertEqual((run["offline"], run["programStatus"]), ("1", "1"))

    def test_explicit_selection_is_checked_against_live_sdk_session_and_recorded(self):
        result = self.launch(thinking="low")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item["thinkingLevel"] for item in self.events("session")], ["low", "low"])
        selection = json.loads((self.base / "selection.json").read_text())
        self.assertEqual(
            selection,
            {
                "state": "ready",
                "model": "fixture-provider/model-with/slash",
                "thinking": "low",
                "session_id": "fixture-session-id",
            },
        )

    def test_unsupported_model_thinking_fails_before_interactive_session(self):
        result = self.launch(mode="unsupported_level", thinking="low")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "Pi selection failed: thinking_unsupported.\n")
        self.assertEqual(
            json.loads((self.base / "selection.json").read_text()),
            {"state": "failed", "reason": "thinking_unsupported"},
        )
        self.assertFalse(self.events("interactive"))

    def test_unavailable_explicit_model_fails_without_fallback(self):
        result = self.launch(mode="missing_model", thinking="low")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "Pi selection failed: model_unavailable.\n")
        self.assertEqual(
            json.loads((self.base / "selection.json").read_text()),
            {"state": "failed", "reason": "model_unavailable"},
        )
        self.assertFalse(self.events("interactive"))

    def test_legacy_package_name_and_public_default_export(self):
        self.manifest(name="@mariozechner/pi-coding-agent", exported={"default": "./sdk.mjs"})
        self.assertEqual(self.launch().returncode, 0)
        self.assertEqual(len(self.events("dispose")), 1)

    def test_recreated_runtime_cannot_change_project_agent_or_session_scope(self):
        for mode in ("changed_cwd", "changed_agent", "changed_session_dir", "changed_session_file"):
            with self.subTest(mode=mode):
                self.log_path.unlink(missing_ok=True)
                result = self.launch(mode)
                self.assert_generic_failure(result)
                self.assertEqual(len(self.events("settings")), 1)
                self.assertEqual(len(self.events("services")), 1)
                self.assertEqual(len(self.events("dispose")), 1)
                self.assertEqual(len(self.events("network_destroy")), 1)

    def test_failures_are_generic_and_existing_runtime_is_disposed(self):
        for mode in ("theme_error", "interactive_error", "run_error", "dispose_error"):
            with self.subTest(mode=mode):
                self.log_path.unlink(missing_ok=True)
                self.assert_generic_failure(self.launch(mode))
                self.assertEqual(len(self.events("dispose")), 1)
                self.assertEqual(len(self.events("network_destroy")), 1)

    def test_missing_wrong_model_invalid_thinking_or_sdk_diagnostic_fail_closed(self):
        for mode in (
            "model_runtime_error",
            "session_runtime_error",
            "missing_model",
            "wrong_model",
            "bad_level",
            "diagnostic_error",
        ):
            with self.subTest(mode=mode):
                self.log_path.unlink(missing_ok=True)
                self.assert_generic_failure(self.launch(mode))
                self.assertFalse(self.events("interactive"))
                self.assertEqual(len(self.events("network_destroy")), 1)

    def test_network_setup_and_cleanup_failures_do_not_expose_secrets(self):
        (self.home / "settings.json").write_text(
            json.dumps(
                {
                    "httpProxy": "http://fixture-user:fixture-secret@proxy.invalid:3128",
                }
            )
        )
        env = {"HTTPS_PROXY": "http://env-user:env-secret@proxy.invalid:3129"}
        for mode in (
            "network_error",
            "network_register_error",
            "network_install_error",
            "network_destroy_error",
        ):
            with self.subTest(mode=mode):
                self.log_path.unlink(missing_ok=True)
                result = self.launch(mode, env_overrides=env)
                self.assert_generic_failure(result)
                self.assertNotIn("fixture-secret", result.stderr)
                self.assertNotIn("env-secret", result.stderr)
                if mode != "network_destroy_error":
                    self.assertFalse(self.events("model_runtime"))
                    self.assertEqual(
                        len(self.events("network_destroy")), 0 if mode == "network_error" else 1
                    )
                else:
                    self.assertEqual(len(self.events("dispose")), 1)
                    self.assertEqual(len(self.events("network_destroy")), 1)

    def test_network_settings_propagate_and_existing_proxy_environment_takes_precedence(self):
        proxy = "http://settings-user:settings-secret@proxy.invalid:3128"
        (self.home / "settings.json").write_text(
            json.dumps(
                {
                    "httpProxy": proxy,
                    "httpIdleTimeoutMs": 23000,
                    "websocketConnectTimeoutMs": 17000,
                    "transport": "sse",
                }
            )
        )
        overrides = {
            "HTTP_PROXY": "http://upper-http.invalid:3129",
            "HTTPS_PROXY": "http://upper-https.invalid:3130",
            "http_proxy": "http://lower-http.invalid:3131",
            "https_proxy": "http://lower-https.invalid:3132",
            "no_proxy": "fixture.invalid",
        }
        cases = [
            (
                {},
                {
                    "HTTP_PROXY": proxy,
                    "HTTPS_PROXY": proxy,
                    "http_proxy": None,
                    "https_proxy": None,
                    "no_proxy": None,
                },
            ),
            (overrides, overrides),
            (
                {"HTTP_PROXY": "", "HTTPS_PROXY": ""},
                {
                    "HTTP_PROXY": "",
                    "HTTPS_PROXY": "",
                    "http_proxy": None,
                    "https_proxy": None,
                    "no_proxy": None,
                },
            ),
        ]
        for environment, expected in cases:
            with self.subTest(environment=bool(environment)):
                self.log_path.unlink(missing_ok=True)
                result = self.launch(env_overrides=environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                agent = self.events("network_agent")[0]
                self.assertEqual(agent["env"], expected)
                self.assertEqual(agent["options"]["bodyTimeout"], 23000)
                self.assertEqual(agent["options"]["headersTimeout"], 23000)
                names = [item["event"] for item in self.all_events()]
                self.assertLess(names.index("network_set_dispatcher"), names.index("model_runtime"))
                self.assertEqual(len(self.events("network_destroy")), 1)
                self.assertNotIn("settings-secret", result.stdout + result.stderr)

    def test_invalid_profile_and_model_fail_before_loading_sdk(self):
        for profile in (
            {},
            {"path": "relative/auth.json"},
            {"path": str(self.home / "auth.json")},
            {"path": None},
            [],
        ):
            with self.subTest(profile=profile):
                self.log_path.unlink(missing_ok=True)
                self.profile(profile)
                self.assert_generic_failure(self.launch())
                self.assertFalse(self.events("model_runtime"))
        self.profile({"path": str(self.auth)})
        for model in ("model-without-provider", "/model", "provider/", "provider/white space"):
            with self.subTest(model=model):
                self.log_path.unlink(missing_ok=True)
                self.assert_generic_failure(self.launch(model=model))
                self.assertFalse(self.events("model_runtime"))

    def test_untrusted_package_or_non_public_export_is_rejected(self):
        for name, exported in (
            ("@fixture/pi", "./sdk.mjs"),
            ("@earendil-works/pi-coding-agent", "../outside.mjs"),
            ("@earendil-works/pi-coding-agent", {"require": "./sdk.mjs"}),
            ("@earendil-works/pi-coding-agent", "./wrong.mjs"),
        ):
            with self.subTest(name=name, exported=exported):
                self.log_path.unlink(missing_ok=True)
                self.manifest(name, exported)
                self.assert_generic_failure(self.launch())
                self.assertFalse(self.events("model_runtime"))


if __name__ == "__main__":
    unittest.main()
