"""Bound entry contracts using disposable projects, without model/network calls."""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_agent_bridge.project_bridge import Project
from herdr_agent_bridge.project_entry import run_bound

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class BoundEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-bound-entry-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.config = self.base / "project.json"
        self.audit = self.base / "calls"
        Project.initialize(
            self.config,
            "generic-project",
            self.project,
            {"bot_id": "fixture-bot", "conversation_id": "fixture-conversation"},
        )

    def call(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_bound(self.config, list(args), audit_dir=self.audit)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_control_commands_preserve_operation_identity_in_entry_audit(self):
        # Exercise the registered wrapper boundary without running a backend.
        for operation in ("interrupt", "native-command", "recovery-status", "recover"):
            with self.subTest(operation=operation):
                code, _, _ = self.call(operation, "--help")
                self.assertEqual(code, 0)
                records = [json.loads(path.read_text()) for path in self.audit.glob("*.json")]
                self.assertEqual({record["operation"] for record in records}, {operation})
                for path in self.audit.glob("*.json"):
                    path.unlink()

    def test_invalid_policy_returns_a_stable_error_code_without_private_details(self):
        code, output, error = self.call("defaults", "--model", "invalid")
        self.assertEqual(code, 1)
        failure = json.loads(error)
        self.assertEqual(failure["code"], "execution_policy_invalid")
        self.assertEqual(failure["operation"], "defaults")
        self.assertNotIn(str(self.config), error)
        self.assertEqual(output, "")

    def test_bound_entry_returns_project_config_and_private_metadata_audit(self):
        code, output, error = self.call("config")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["project_id"], "generic-project")
        records = list(self.audit.glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(
            (record["operation"], record["phase"], record["returncode"]), ("config", "returned", 0)
        )
        self.assertTrue(record["ancestry"])
        self.assertEqual(stat.S_IMODE(records[0].stat().st_mode), 0o600)
        self.assertNotIn("stdout", record)
        self.assertNotIn("stderr", record)
        self.assertNotIn("argv", record)

    def test_new_requests_and_late_callbacks_keep_their_own_runs(self):
        fixture_bin = self.base / "bin"
        fixture_bin.mkdir()
        state = self.base / "hc-state.json"
        log = self.base / "hc-calls.jsonl"
        executable = fixture_bin / "hc"
        executable.write_text(
            f"#!{sys.executable}\n"
            + """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
state = Path(os.environ['BOUND_TEST_HC_STATE'])
data = json.loads(state.read_text()) if state.exists() else {'terminals': {}, 'next': 0}
with open(os.environ['BOUND_TEST_HC_LOG'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
command = args[0]
if command == 'run':
    data['next'] += 1
    address = 'fixture-' + str(data['next'])
    name = args[args.index('--name') + 1]
    label, value = args[args.index('--label') + 1].split('=', 1)
    terminal = {'address': address, 'name': name, 'labels': {label: value},
                'state': 'live', 'turn': 0, 'status': {'state': 'idle'}, 'created_ms': 0}
    data['terminals'][address] = terminal
    selection = next(arg.split('=', 1)[1] for arg in args if arg.startswith('HAB_PI_SELECTION_FILE='))
    Path(selection).write_text(json.dumps({'state': 'ready', 'session_id': address,
        'model': 'openai/gpt-6.1-sol', 'thinking': 'high'}))
    result = {'address': address}
elif command == 'get':
    result = data['terminals'].get(args[-1])
    if result is None:
        result = next((t for t in data['terminals'].values() if t['name'] == args[-1]), None)
    if result is None:
        print(json.dumps({'error': {'kind': 'not_found'}}), file=sys.stderr)
        raise SystemExit(3)
elif command == 'send':
    result = {'started': True}
elif command == 'read':
    result = {'text': 'fixture'}
elif command == 'kill':
    data['terminals'][args[1]]['state'] = 'finished'
    result = {}
elif command == 'rm':
    del data['terminals'][args[1]]
    result = {}
else:
    raise SystemExit(99)
state.write_text(json.dumps(data))
print(json.dumps(result))
"""
        )
        executable.chmod(0o700)
        with patch.dict(
            os.environ,
            {
                "PATH": str(fixture_bin) + os.pathsep + os.environ["PATH"],
                "BOUND_TEST_HC_STATE": str(state),
                "BOUND_TEST_HC_LOG": str(log),
            },
        ):

            def cli(*args, expected=0):
                code, out, error = self.call(*args)
                self.assertEqual(code, expected, error)
                return json.loads(out if code == 0 else error)

            first = cli("start", "first-new-request")
            self.assertEqual(cli("start", "first-new-request")["run_id"], first["run_id"])
            cli("start", "second-new-request", expected=1)
            ready = cli("status", "--request-id", "first-new-request", "--observe")
            notification = json.loads(
                (self.project / first["run"] / "notifications.json").read_text()
            )
            payload = notification["events"][ready["event_id"]]["payload"]
            callback = self.base / "original-event.json"
            callback.write_text(json.dumps(payload))
            prompt = self.base / "arbitrary-prompt.txt"
            prompt.write_text("PRIVATE-FIXTURE-PROMPT Inspect this different authorized task")
            submitted = cli(
                "submit",
                "arbitrary-round",
                "--request-id",
                "first-new-request",
                "--prompt-file",
                str(prompt),
            )
            self.assertEqual(submitted["task"]["task_id"], "arbitrary-round")
            self.assertTrue(cli("close", "--request-id", "first-new-request")["closed"])
            second = cli("start", "second-new-request")
            self.assertNotEqual(first["run_id"], second["run_id"])
            late = cli("notification", "--payload-file", str(callback))
            self.assertEqual(late["request_id"], "first-new-request")
            self.assertTrue(late["closed"])
            self.assertEqual(cli("status")["request_id"], "second-new-request")
            self.assertTrue(cli("close", "--request-id", "second-new-request")["closed"])
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(sum(args[0] == "run" for args in calls), 2)
        self.assertEqual(sum(args[0] == "send" for args in calls), 1)
        self.assertNotIn(
            "PRIVATE-FIXTURE-PROMPT", "".join(f.read_text() for f in self.audit.glob("*.json"))
        )


if __name__ == "__main__":
    unittest.main()
