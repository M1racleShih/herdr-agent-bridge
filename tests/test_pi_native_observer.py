"""Exercise the native Pi observer through disposable extension events."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_agent_bridge.resource_paths import resource_path

SCRIPT = resource_path("pi_native_observer.mjs")
NODE = shutil.which("node")

HARNESS = r"""
import { readFileSync, existsSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const scenario = JSON.parse(readFileSync(0, 'utf8'));
const handlers = {};
let entries = [];
let sessionName = null;
let thinking = Object.hasOwn(scenario, 'thinking') ? scenario.thinking : 'high';
let model = Object.hasOwn(scenario, 'model') ? scenario.model : {provider: 'fixture', id: 'model/submodel'};
const pi = {
    on(name, handler) { (handlers[name] ??= []).push(handler); },
    getThinkingLevel: () => {
        if (scenario.thinking_error) throw new Error('fixture-secret');
        return thinking;
    },
    getCommands: () => scenario.commands ?? [],
    getAllTools: () => scenario.tools ?? [],
};
if (scenario.omit_resources) delete pi.getCommands;
const ctx = {
    get model() { return model; },
    sessionManager: {
        getSessionId: () => Object.hasOwn(scenario, 'session_id') ? scenario.session_id : 'fixture-session',
        getEntries: () => entries,
        getSessionName: () => sessionName,
    },
};
const observer = (await import(pathToFileURL(process.env.FIXTURE_OBSERVER))).default;
await observer(pi);
const snapshots = [];
for (const item of scenario.events ?? [{type: 'session_start'}]) {
    entries = item.entries ?? entries;
    if (Object.hasOwn(item, 'name')) sessionName = item.name;
    if (Object.hasOwn(item, 'model')) model = item.model;
    if (Object.hasOwn(item, 'thinking')) thinking = item.thinking;
    for (const handler of handlers[item.type] ?? []) await handler(item, ctx);
    const read = (path) => existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : null;
    snapshots.push({selection: read(process.env.HAB_PI_SELECTION_FILE),
        resources: read(process.env.HAB_PI_RESOURCES_FILE),
        settled: read(process.env.HAB_PI_SETTLED_FILE),
        name: read(join(dirname(process.env.HAB_PI_SELECTION_FILE), 'pi-name.json'))});
}
process.stdout.write(JSON.stringify(snapshots));
"""


@unittest.skipUnless(NODE, "Node.js is required")
class NativeObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def observe(self, scenario=None, model="fixture/model/submodel", thinking="high"):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "FIXTURE_OBSERVER": str(SCRIPT),
            "HAB_PI_SELECTION_FILE": str(self.base / "selection.json"),
            "HAB_PI_RESOURCES_FILE": str(self.base / "resources.json"),
            "HAB_PI_SETTLED_FILE": str(self.base / "settled.json"),
            "HAB_PI_MODEL": model,
            "HAB_PI_THINKING": thinking,
        }
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", HARNESS],
            input=json.dumps(scenario or {}),
            text=True,
            capture_output=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_records_the_effective_startup_selection(self):
        actual = self.observe()[0]["selection"]
        self.assertEqual(
            actual,
            {
                "state": "ready",
                "model": "fixture/model/submodel",
                "thinking": "high",
                "session_id": "fixture-session",
            },
        )
        self.assertEqual((self.base / "selection.json").stat().st_mode & 0o777, 0o600)

    def test_session_name_changes_are_observed_without_a_model_turn(self):
        before, after = self.observe(
            {
                "events": [
                    {"type": "session_start"},
                    {"type": "session_info_changed", "name": "QA native session"},
                ]
            }
        )
        self.assertIsNone(before["name"]["name"])
        self.assertEqual(after["name"]["session_id"], "fixture-session")
        self.assertEqual(after["name"]["name"], "QA native session")
        self.assertNotEqual(before["name"]["revision"], after["name"]["revision"])
        self.assertEqual(before["settled"], after["settled"])

    def test_rejects_model_or_effective_thinking_mismatches(self):
        for scenario in (
            {"model": {"provider": "other", "id": "model/submodel"}},
            {"thinking": "medium"},
        ):
            with self.subTest(scenario=scenario):
                self.assertEqual(
                    self.observe(scenario)[0]["selection"],
                    {"state": "failed", "reason": "selection_mismatch"},
                )

    def test_unavailable_selection_is_recorded_without_error_details(self):
        for scenario in (
            {"model": None},
            {"thinking_error": True},
            {"session_id": None},
            {"thinking": "fixture-secret"},
        ):
            with self.subTest(scenario=scenario):
                selection = self.observe(scenario)[0]["selection"]
                self.assertEqual(selection["state"], "failed")
                self.assertNotIn("fixture-secret", json.dumps(selection))

    def test_empty_requested_parameters_keep_the_cli_defaults(self):
        selection = self.observe(
            {"model": {"provider": "default", "id": "model"}, "thinking": "medium"},
            model="",
            thinking="",
        )[0]["selection"]
        self.assertEqual(
            selection,
            {
                "state": "ready",
                "model": "default/model",
                "thinking": "medium",
                "session_id": "fixture-session",
            },
        )

    def test_invalid_requested_parameters_fail_closed(self):
        for model, thinking in [("invalid", "high"), ("fixture/model/submodel", "invalid")]:
            with self.subTest(model=model, thinking=thinking):
                self.assertEqual(
                    self.observe(model=model, thinking=thinking)[0]["selection"],
                    {"state": "failed", "reason": "startup_failed"},
                )

    def test_later_model_or_thinking_changes_invalidate_a_pinned_selection(self):
        for event in (
            {"type": "model_select", "model": {"provider": "other", "id": "model"}},
            {"type": "thinking_level_select", "thinking": "low"},
        ):
            with self.subTest(event=event):
                snapshots = self.observe({"events": [{"type": "session_start"}, event]})
                self.assertEqual(snapshots[0]["selection"]["state"], "ready")
                self.assertEqual(
                    snapshots[1]["selection"], {"state": "failed", "reason": "selection_mismatch"}
                )

    def test_resource_snapshot_contains_only_observed_safe_paths(self):
        fixture = {
            "commands": [
                {
                    "source": "skill",
                    "name": "fixture-secret",
                    "description": "fixture-secret",
                    "sourceInfo": {"path": "/global/skills/SKILL.md", "source": "fixture-secret"},
                },
                {"source": "prompt", "sourceInfo": {"path": "/project/prompts/example.md"}},
                {"source": "extension", "sourceInfo": {"path": "/project/ext.js"}},
                {"source": "extension", "sourceInfo": {"path": "/project/ext.js"}},
                {"source": "extension", "sourceInfo": {"path": "https://fixture-secret.invalid/"}},
            ],
            "tools": [
                {
                    "name": "fixture-secret",
                    "sourceInfo": {"path": "builtin:mcp"},
                    "parameters": {"token": "fixture-secret"},
                },
                {"sourceInfo": {"path": "/global/tools.js"}},
            ],
        }
        resources = self.observe(fixture)[0]["resources"]
        self.assertEqual(
            resources,
            {
                "state": "partial",
                "observed_at": "session_start",
                "command_paths": {
                    "extension": ["/project/ext.js"],
                    "prompt": ["/project/prompts/example.md"],
                    "skill": ["/global/skills/SKILL.md"],
                },
                "tool_paths": ["/global/tools.js", "builtin:mcp"],
                "truncated": False,
            },
        )
        self.assertNotIn("fixture-secret", json.dumps(resources))

    def test_unavailable_resource_api_is_not_reported_as_an_empty_inventory(self):
        self.assertEqual(
            self.observe({"omit_resources": True})[0]["resources"],
            {"state": "unavailable", "observed_at": "session_start"},
        )

    def test_only_agent_settled_confirms_the_new_assistant_entry(self):
        user = {"type": "message", "id": "user-1", "message": {"role": "user"}}
        final = {
            "type": "message",
            "id": "assistant-1",
            "message": {"role": "assistant", "content": "fixture-secret"},
        }
        snapshots = self.observe(
            {
                "events": [
                    {"type": "session_start"},
                    {"type": "agent_start", "entries": [user]},
                    {"type": "agent_end", "entries": [user, final]},
                    {"type": "agent_settled", "entries": [user, final], "aborted": False},
                ]
            }
        )
        self.assertEqual(
            snapshots[0]["settled"], {"state": "starting", "session_id": "fixture-session"}
        )
        self.assertEqual(
            snapshots[1]["settled"], {"state": "working", "session_id": "fixture-session"}
        )
        self.assertEqual(snapshots[2]["settled"], snapshots[1]["settled"])
        self.assertEqual(
            snapshots[3]["settled"],
            {
                "state": "settled",
                "session_id": "fixture-session",
                "final_message_id": "assistant-1",
                "aborted": False,
            },
        )
        self.assertNotIn("fixture-secret", json.dumps(snapshots))

    def test_aborted_missing_and_nonfinal_messages_never_reuse_a_previous_answer(self):
        previous = {"type": "message", "id": "old", "message": {"role": "assistant"}}
        final = {"type": "message", "id": "new", "message": {"role": "assistant"}}
        tool = {"type": "message", "id": "tool", "message": {"role": "toolResult"}}
        for entries, aborted in [
            ([previous], False),
            ([previous, final], True),
            ([previous, final, tool], False),
        ]:
            with self.subTest(entries=entries, aborted=aborted):
                snapshots = self.observe(
                    {
                        "events": [
                            {"type": "session_start"},
                            {"type": "agent_start", "entries": [previous]},
                            {"type": "agent_settled", "entries": entries, "aborted": aborted},
                        ]
                    }
                )
                self.assertEqual(
                    snapshots[-1]["settled"],
                    {
                        "state": "settled",
                        "session_id": "fixture-session",
                        "final_message_id": None,
                        "aborted": aborted,
                        **({"aborted_message_id": "new"} if aborted else {}),
                    },
                )


if __name__ == "__main__":
    unittest.main()
