"""Exercise Pi's network helper with fake Undici; no network or Pi is used."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_agent_bridge.resource_paths import resource_path

SCRIPT = resource_path("pi_network.mjs")
NODE = shutil.which("node")


FAKE_UNDICI = r"""
const { EventEmitter } = require('node:events');
const { writeFileSync } = require('node:fs');
writeFileSync(process.env.FAKE_LOAD_MARKER, 'loaded');
const events = globalThis.__fakeEvents;
const fail = (text = 'fixture secret failure') => { throw new Error(text); };
const mode = process.env.FAKE_UNDICI_MODE;
events.push({op: 'load', module: __filename});
let active = {kind: 'previous'};

class Client extends EventEmitter {
  constructor(origin, options) {
    super();
    events.push({op: 'client', origin, connections: options.connections});
    if (mode === 'client_fail') fail('fixture request rejected');
    this.kind = 'client';
  }
  request() {
    this.emit('error', new Error('fixture event error'));
    return Promise.reject(new Error('fixture request rejected'));
  }
}
class Pool extends EventEmitter {
  constructor(origin, options) {
    super();
    events.push({op: 'pool', origin, connections: options.connections,
      factory: typeof options.factory, marker: options.marker});
    if (mode === 'pool_fail') fail('fixture pool rejected');
    this.kind = 'pool';
    this.options = options;
  }
}
class EnvHttpProxyAgent extends EventEmitter {
  constructor(options) {
    super();
    events.push({op: 'agent', options: {allowH2: options.allowH2,
      proxyTunnel: options.proxyTunnel, bodyTimeout: options.bodyTimeout,
      headersTimeout: options.headersTimeout, connect: options.connect,
      factory: typeof options.factory, clientFactory: typeof options.clientFactory},
      env: {HTTP_PROXY: process.env.HTTP_PROXY ?? null, HTTPS_PROXY: process.env.HTTPS_PROXY ?? null,
        http_proxy: process.env.http_proxy ?? null, https_proxy: process.env.https_proxy ?? null,
        NO_PROXY: process.env.NO_PROXY ?? null, no_proxy: process.env.no_proxy ?? null}});
    if (mode === 'agent_fail') fail();
    this.kind = 'agent';
    this.options = options;
  }
  destroy() { events.push({op: 'destroy', kind: this.kind}); return Promise.resolve(); }
}
function getGlobalDispatcher() { return active; }
function setGlobalDispatcher(value) {
  events.push({op: 'set', kind: value.kind});
  active = value;
  if (mode === 'set_fail' && value.kind === 'agent') fail();
}
function install() {
  events.push({op: 'install'});
  globalThis.fetch = function fixtureInstalledFetch() {};
  if (mode === 'install_fail') fail();
}
module.exports = {Client, Pool, EnvHttpProxyAgent, getGlobalDispatcher, setGlobalDispatcher, install};
"""


RUNNER = r"""
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const config = JSON.parse(readFileSync(0, 'utf8'));
for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy']) {
  delete process.env[key];
}
for (const [key, value] of Object.entries(config.env || {})) process.env[key] = value;
process.env.FAKE_UNDICI_MODE = config.mode || '';
globalThis.__fakeEvents = [];
const originalFetch = globalThis.fetch;
const { configurePiNetwork } = await import(pathToFileURL(config.helper).href);
let custom;
if (config.action === 'override') {
  custom = function deliberateCustomFetch() {};
  globalThis.fetch = custom;
}
if (config.action === 'getter_throw') {
  config.settings = Object.defineProperty({}, 'httpProxy', {get() { throw new Error('fixture secret failure'); }});
}
const result = {};
try {
  const agent = configurePiNetwork(config.sdk, config.settings);
  result.completed = true;
  result.agent = agent.kind;
  result.customPreserved = custom ? globalThis.fetch === custom : undefined;
  if (config.action === 'factories') {
    agent.emit('error', new Error('fixture agent event error'));
    const client = agent.options.clientFactory('https://fixture.invalid', {connections: 1});
    const direct = agent.options.factory('https://fixture.invalid', {connections: 1});
    const pool = agent.options.factory('https://fixture.invalid', {connections: 3, marker: 'preserved'});
    pool.emit('error', new Error('fixture pool event error'));
    const nested = pool.options.factory('https://fixture.invalid', {connections: 1});
    direct.emit('error', new Error('fixture client event error'));
    nested.emit('error', new Error('fixture nested event error'));
    try { await client.request(); } catch (error) { result.requestError = error.message; }
  }
  if (config.action === 'client_failure' || config.action === 'pool_failure') {
    try {
      agent.options.factory('https://fixture.invalid', {connections: config.action === 'client_failure' ? 1 : 2});
      result.factoryError = 'not rejected';
    } catch (error) { result.factoryError = error.message; }
  }
  if (config.action === 'fetch_sequence') {
    const installed = globalThis.fetch;
    const second = configurePiNetwork(config.sdk, config.settings);
    const override = function deliberateFetchOverride() {};
    globalThis.fetch = override;
    const third = configurePiNetwork(config.sdk, config.settings);
    result.overridePreserved = globalThis.fetch === override;
    globalThis.fetch = originalFetch;
    const fourth = configurePiNetwork(config.sdk, config.settings);
    result.reinstalled = globalThis.fetch !== originalFetch && globalThis.fetch !== installed;
    await second.destroy();
    await third.destroy();
    await fourth.destroy();
  }
  await agent.destroy();
} catch (error) {
  result.completed = false;
  result.error = error.message;
}
result.events = globalThis.__fakeEvents;
result.fetchRestored = globalThis.fetch === originalFetch;
result.env = Object.fromEntries(['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy']
  .map(key => [key, process.env[key] ?? null]));
process.stdout.write(JSON.stringify(result));
"""


@unittest.skipUnless(NODE, "node is required for the Pi network helper")
class PiNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-pi-network-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sdk = self.base / "sdk" / "index.mjs"
        self.sdk.parent.mkdir()
        self.sdk.write_text("// synthetic SDK; never imported\n")
        self.undici = self.sdk.parent / "node_modules" / "undici"
        self.undici.mkdir(parents=True)
        (self.undici / "package.json").write_text(
            json.dumps({"name": "undici", "main": "./index.cjs"})
        )
        (self.undici / "index.cjs").write_text(FAKE_UNDICI)
        self.runner = self.base / "runner.mjs"
        self.runner.write_text(RUNNER)
        self.marker = self.base / "loaded.txt"

    def run_fixture(self, **settings):
        self.marker.unlink(missing_ok=True)
        config = {"sdk": str(self.sdk), "helper": str(SCRIPT), **settings}
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.base),
            "FAKE_LOAD_MARKER": str(self.marker),
        }
        result = subprocess.run(
            [NODE, str(self.runner)],
            input=json.dumps(config),
            text=True,
            capture_output=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    @staticmethod
    def events(result, op):
        return [item for item in result["events"] if item["op"] == op]

    def test_proxy_environment_precedence_and_no_proxy_are_preserved(self):
        result = self.run_fixture(
            settings={"httpProxy": "  http://settings-proxy.invalid:8080  "},
            env={
                "HTTP_PROXY": "",
                "http_proxy": "http://lower.invalid",
                "https_proxy": "http://lower-secure.invalid",
                "NO_PROXY": "local.invalid",
                "no_proxy": "127.0.0.1",
            },
        )
        self.assertTrue(result["completed"])
        self.assertEqual(
            result["env"],
            {
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "http://settings-proxy.invalid:8080",
                "http_proxy": "http://lower.invalid",
                "https_proxy": "http://lower-secure.invalid",
                "NO_PROXY": "local.invalid",
                "no_proxy": "127.0.0.1",
            },
        )
        self.assertEqual(self.events(result, "agent")[0]["env"], result["env"])
        result = self.run_fixture(
            settings={"httpProxy": "http://settings.invalid"},
            env={
                "HTTP_PROXY": "http://upper.invalid",
                "HTTPS_PROXY": "http://upper-secure.invalid",
            },
        )
        self.assertEqual(result["env"]["HTTP_PROXY"], "http://upper.invalid")
        self.assertEqual(result["env"]["HTTPS_PROXY"], "http://upper-secure.invalid")
        result = self.run_fixture(settings={"httpProxy": "   "})
        self.assertIsNone(result["env"]["HTTP_PROXY"])
        self.assertIsNone(result["env"]["HTTPS_PROXY"])

    def test_timeout_normalization_and_dispatcher_options(self):
        for settings, expected in (
            ({}, 300000),
            ({"httpIdleTimeoutMs": 1.99}, 1),
            ({"httpIdleTimeoutMs": " 200.8 "}, 200),
            ({"httpIdleTimeoutMs": " DISABLED "}, 0),
            ({"httpIdleTimeoutMs": "1e3"}, 1000),
            ({"httpIdleTimeoutMs": 0}, 0),
        ):
            with self.subTest(settings=settings):
                result = self.run_fixture(settings=settings)
                self.assertTrue(result["completed"])
                options = self.events(result, "agent")[0]["options"]
                self.assertEqual(
                    options,
                    {
                        "allowH2": False,
                        "proxyTunnel": True,
                        "bodyTimeout": expected,
                        "headersTimeout": expected,
                        "connect": {"autoSelectFamilyAttemptTimeout": 2000},
                        "factory": "function",
                        "clientFactory": "function",
                    },
                )
                self.assertEqual(len(self.events(result, "set")), 1)
                self.assertEqual(len(self.events(result, "destroy")), 1)

    def test_malformed_settings_fail_before_loading_undici_or_changing_environment(self):
        invalid = [
            None,
            [],
            {"httpIdleTimeoutMs": None},
            {"httpIdleTimeoutMs": True},
            {"httpIdleTimeoutMs": -1},
            {"httpIdleTimeoutMs": ""},
            {"httpIdleTimeoutMs": "nan"},
            {"httpIdleTimeoutMs": "Infinity"},
            {"httpIdleTimeoutMs": {}},
            {"httpProxy": False},
            {"httpProxy": 5},
        ]
        for settings in invalid:
            with self.subTest(settings=settings):
                result = self.run_fixture(
                    settings=settings, env={"http_proxy": "http://existing.invalid"}
                )
                self.assertFalse(result["completed"])
                self.assertEqual(result["error"], "Invalid Pi network settings")
                self.assertFalse(self.marker.exists())
                self.assertEqual(result["events"], [])
                self.assertIsNone(result["env"]["HTTP_PROXY"])
                self.assertEqual(result["env"]["http_proxy"], "http://existing.invalid")
        result = self.run_fixture(action="getter_throw")
        self.assertEqual(result["error"], "Invalid Pi network settings")
        self.assertFalse(self.marker.exists())

    def test_intentional_fetch_override_and_helper_reinstallation(self):
        result = self.run_fixture(action="override", settings={})
        self.assertTrue(result["customPreserved"])
        self.assertEqual(self.events(result, "install"), [])
        result = self.run_fixture(action="fetch_sequence", settings={})
        self.assertTrue(result["overridePreserved"])
        self.assertTrue(result["reinstalled"])
        self.assertEqual(len(self.events(result, "install")), 3)
        self.assertEqual(len(self.events(result, "destroy")), 4)

    def test_client_pool_and_agent_errors_do_not_swallow_request_rejection(self):
        result = self.run_fixture(action="factories", settings={})
        self.assertTrue(result["completed"])
        self.assertEqual(result["requestError"], "fixture request rejected")
        self.assertEqual(len(self.events(result, "client")), 3)
        self.assertEqual(
            self.events(result, "pool"),
            [
                {
                    "op": "pool",
                    "origin": "https://fixture.invalid",
                    "connections": 3,
                    "factory": "function",
                    "marker": "preserved",
                }
            ],
        )
        for action, mode, expected in (
            ("client_failure", "client_fail", "fixture request rejected"),
            ("pool_failure", "pool_fail", "fixture pool rejected"),
        ):
            with self.subTest(mode=mode):
                result = self.run_fixture(action=action, mode=mode, settings={})
                self.assertEqual(result["factoryError"], expected)

    def test_initialization_errors_are_generic_and_rollback_owned_state(self):
        for mode in ("agent_fail", "set_fail", "install_fail"):
            with self.subTest(mode=mode):
                result = self.run_fixture(
                    mode=mode, settings={"httpProxy": "http://fixture-secret.invalid"}
                )
                self.assertFalse(result["completed"])
                self.assertEqual(result["error"], "Pi network initialization failed")
                self.assertIsNone(result["env"]["HTTP_PROXY"])
                self.assertIsNone(result["env"]["HTTPS_PROXY"])
                self.assertTrue(result["fetchRestored"])
                if mode != "agent_fail":
                    self.assertEqual(
                        [event["kind"] for event in self.events(result, "set")],
                        ["agent", "previous"],
                    )
                    self.assertEqual(len(self.events(result, "destroy")), 1)

    def test_dependency_resolves_from_real_sdk_entry_and_invalid_paths_fail_closed(self):
        rogue = self.base / "rogue"
        rogue.mkdir()
        (rogue / "node_modules" / "undici").mkdir(parents=True)
        (rogue / "node_modules" / "undici" / "package.json").write_text('{"main":"index.cjs"}')
        (rogue / "node_modules" / "undici" / "index.cjs").write_text(
            "throw new Error('wrong package');"
        )
        link = rogue / "linked-sdk.mjs"
        link.symlink_to(self.sdk)
        result = self.run_fixture(sdk=str(link), settings={})
        self.assertTrue(result["completed"])
        self.assertEqual(Path(self.events(result, "load")[0]["module"]), self.undici / "index.cjs")
        for path in ("relative-sdk.mjs", str(self.base / "missing-sdk.mjs"), str(self.base)):
            with self.subTest(path=path):
                result = self.run_fixture(
                    sdk=path, settings={"httpProxy": "http://settings.invalid"}
                )
                self.assertFalse(result["completed"])
                self.assertEqual(result["error"], "Pi network initialization failed")
                self.assertFalse(self.marker.exists())
                self.assertIsNone(result["env"]["HTTP_PROXY"])
                self.assertIsNone(result["env"]["HTTPS_PROXY"])


if __name__ == "__main__":
    unittest.main()
