"""Run and evidence paths preserve bounded new/legacy filesystem contracts."""

import tempfile
import unittest
from pathlib import Path

from herdr_agent_bridge.bridge_common import BridgeError
from herdr_agent_bridge.grok_bridge import Manager
from herdr_agent_bridge.runtime_paths import (
    LEGACY_RUNS,
    RUNS,
    acceptance_directory,
    new_manager_run,
    valid_run_pointer,
)


class RuntimePathTests(unittest.TestCase):
    def test_new_manager_refuses_historical_destinations_before_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            target = project / LEGACY_RUNS / "old-destination"
            calls = []
            with self.assertRaises(BridgeError):
                Manager.create(
                    target,
                    project,
                    {"bot_id": "fixture", "conversation_id": "fixture"},
                    runner=lambda *args: calls.append(args),
                )
            self.assertFalse(target.exists())
            self.assertEqual(calls, [])

    def test_legacy_reading_keeps_the_same_pointer_restrictions(self):
        for base in (RUNS, LEGACY_RUNS):
            self.assertTrue(
                valid_run_pointer(str(base / "saved-run"), legacy=True, project_entry=True)
            )
            self.assertTrue(valid_run_pointer(str(base / "group" / "run"), legacy=True))
            self.assertFalse(
                valid_run_pointer(str(base / "group" / "run"), legacy=True, project_entry=True)
            )
            for suffix in ("", "/", "/../escape", "/./run", "//run", "/run\nprivate", "/run\x00"):
                with self.subTest(base=base, suffix=suffix):
                    self.assertFalse(valid_run_pointer(str(base) + suffix, legacy=True))
        self.assertFalse(valid_run_pointer(str(LEGACY_RUNS / "old")))
        for pointer in ("/absolute", "other/run", ".artifacts/other/run", "a\\b", None):
            self.assertFalse(valid_run_pointer(pointer, legacy=True))

    def test_new_run_and_acceptance_paths_reject_symlink_ancestors(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            outside = Path(directory) / "outside"
            project.mkdir()
            outside.mkdir()
            (project / ".artifacts").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(BridgeError):
                new_manager_run(project / RUNS / "run", project)
            with self.assertRaises(BridgeError):
                acceptance_directory(project, "pi", "check")
            self.assertEqual(list(outside.iterdir()), [])

    def test_acceptance_directories_are_private_unique_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for kind in ("codex", "pi", "core-features"):
                path = acceptance_directory(project, kind, "owned-check")
                self.assertEqual(path, project / ".artifacts/acceptance" / kind / "owned-check")
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                with self.assertRaises(FileExistsError):
                    acceptance_directory(project, kind, "owned-check")
            for name in ("../escape", "/absolute", "group/run", "", "line\nbreak"):
                with self.assertRaises(BridgeError):
                    acceptance_directory(project, "pi", name)
