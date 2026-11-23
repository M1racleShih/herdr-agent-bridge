"""Installed CLI and module contract; disposable configs, no backend/model calls."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class InstalledCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="hab-cli-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "project.json"
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PYTHONPATH", "PYTHONHOME")
        }

    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "herdr_agent_bridge", *map(str, args)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_version_help_and_parameter_exit_codes(self):
        version = self.invoke("--version")
        self.assertEqual((version.returncode, version.stderr), (0, ""))
        self.assertEqual(version.stdout.strip(), "herdr-bridge 0.1.0")
        for args in [
            ("--help",),
            ("manager", "--help"),
            ("agent", "--help"),
            ("app", "--help"),
            ("tool", "smoke_hc", "--help"),
        ]:
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertIn("usage:", result.stdout)
        refused = self.invoke("--config", self.config, "not-a-command")
        self.assertEqual((refused.returncode, refused.stdout), (2, ""))

    def test_codex_acceptance_commands_use_responsibility_names(self):
        for name in ("accept_codex", "render_codex_report"):
            with self.subTest(name=name):
                result = self.invoke("tool", name, "--help")
                self.assertEqual((result.returncode, result.stderr), (0, ""))
        for name in ("accept_phase1", "render_phase1_report"):
            with self.subTest(removed=name):
                result = self.invoke("tool", name, "--help")
                self.assertEqual(result.returncode, 2)

    def test_project_json_and_old_config_read_without_checkout(self):
        result = self.invoke(
            "--config",
            self.config,
            "init",
            "--project-id",
            "installed-test",
            "--project",
            self.root,
            "--bot-id",
            "fixture-bot",
            "--conversation-id",
            "fixture-conversation",
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(json.loads(result.stdout)["project_id"], "installed-test")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        data = json.loads(self.config.read_text())
        data.pop("runtime")
        self.config.write_text(json.dumps(data))
        old = self.invoke("--config", self.config, "config")
        self.assertEqual(old.returncode, 0)
        self.assertEqual(json.loads(old.stdout)["runtime"], "isolated")
        self.assertEqual(json.loads(self.config.read_text()), data)
        failed = self.invoke("--config", self.config, "result")
        self.assertEqual((failed.returncode, failed.stdout), (1, ""))
        self.assertEqual(json.loads(failed.stderr)["error"], "project_operation_failed")
        self.assertNotIn(str(self.root), failed.stderr)

    @unittest.skipUnless(os.name == "posix", "FIFO cancellation check needs POSIX")
    def test_sigterm_is_unknown_delivery_for_cli_and_legacy_manager(self):
        root = Path(__file__).resolve().parents[1]
        for prefix in (
            [sys.executable, "-m", "herdr_agent_bridge", "manager"],
            [sys.executable, str(root / "scripts/grok_bridge.py")],
        ):
            with self.subTest(prefix=prefix):
                run = self.root / ("run" + str(len(list(self.root.iterdir()))))
                run.mkdir()
                fifo = run / "manager.json"
                os.mkfifo(fifo)
                child = subprocess.Popen(
                    [
                        *prefix,
                        "--run",
                        str(run),
                        "--bot-id",
                        "fixture",
                        "--conversation-id",
                        "fixture",
                        "status",
                    ],
                    cwd=self.root,
                    env=self.env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                writer = None
                try:
                    deadline = time.monotonic() + 5
                    while writer is None and time.monotonic() < deadline:
                        try:
                            writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                        except OSError:
                            self.assertIsNone(child.poll())
                            time.sleep(0.01)
                    self.assertIsNotNone(writer, "manager must reach the dedicated FIFO")
                    child.send_signal(signal.SIGTERM)
                    stdout, stderr = child.communicate(timeout=5)
                    self.assertEqual((child.returncode, stdout), (1, ""))
                    self.assertEqual(json.loads(stderr)["delivery"], "unknown")
                    self.assertEqual(json.loads(stderr)["error"], "KeyboardInterrupt")
                finally:
                    if writer is not None:
                        os.close(writer)
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)
