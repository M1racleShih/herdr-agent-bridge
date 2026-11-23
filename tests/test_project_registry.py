"""Project selection and registration against disposable local directories."""

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from herdr_agent_bridge.bridge_common import BridgeError
from herdr_agent_bridge.project_registry import ProjectRegistry


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.allowed = self.root / "work"
        self.allowed.mkdir()
        self.store = self.root / "private/catalog.json"
        self.registry = ProjectRegistry(self.store, allowed_roots=[self.allowed])

    def repo(self, relative, *, git_file=False):
        path = self.allowed / relative
        path.mkdir(parents=True)
        if git_file:
            (path / ".git").write_text("gitdir: /not/read/by/discovery\n")
        else:
            (path / ".git").mkdir()
        return path

    def test_first_lookup_requires_confirmation_then_uses_the_registered_id(self):
        path = self.repo("report-app")
        first = self.registry.resolve("report-app")
        self.assertEqual(first["status"], "needs_confirmation")
        self.assertEqual([m["root"] for m in first["matches"]], [str(path)])
        self.assertFalse(self.store.parent.exists())
        self.assertFalse((path / ".artifacts").exists())

        registered = self.registry.register_confirmed(path)
        reopened = ProjectRegistry(self.store, allowed_roots=[self.allowed])
        for selector in ("Report-App", registered["project_id"]):
            result = reopened.resolve(selector)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(result["matches"], [registered])
        before = self.store.read_bytes()
        self.assertEqual(reopened.register_confirmed(path), registered)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertFalse((path / ".artifacts").exists())

    def test_same_name_is_ambiguous_until_a_registered_project_can_be_selected(self):
        first, second = self.repo("client-a/report"), self.repo("client-b/report")
        selection = self.registry.resolve("report")
        self.assertEqual(selection["status"], "ambiguous")
        self.assertEqual([item["root"] for item in selection["matches"]], [str(first), str(second)])
        self.assertFalse(self.store.exists())
        chosen = self.registry.register_confirmed(second)
        self.assertEqual(self.registry.resolve("report")["matches"], [chosen])
        self.registry.register_confirmed(first)
        self.assertEqual(self.registry.resolve("report")["status"], "ambiguous")
        self.assertEqual(self.registry.resolve(chosen["project_id"])["matches"], [chosen])

    def test_scan_stops_at_project_roots_and_checks_git_file_without_opening_it(self):
        tree = self.repo("worktree", git_file=True)
        (tree / ".git").chmod(0)
        repo = self.repo("repo")
        self.repo("repo/nested")
        self.repo("vendor/unwanted")
        self.repo("node_modules/unwanted")
        self.repo(".venv/unwanted")
        self.repo("dist/unwanted")
        non_git = self.allowed / "non-git"
        non_git.mkdir()
        found = self.registry.discover()
        self.assertEqual(
            {x["root"]: x["marker"] for x in found["candidates"]},
            {str(tree): "git_file", str(repo): "git_directory"},
        )
        self.assertEqual(self.registry.resolve("non-git")["status"], "not_found")
        manual = self.registry.register_confirmed(non_git)
        self.assertEqual(self.registry.resolve("non-git")["matches"], [manual])
        registered_git = self.registry.register_confirmed(tree)
        found_after = {x["root"]: x for x in self.registry.discover()["candidates"]}
        self.assertEqual(found_after[str(tree)]["project_id"], registered_git["project_id"])
        self.assertIsNone(found_after[str(repo)]["project_id"])

    def test_scan_reports_depth_candidate_and_entry_limits(self):
        self.repo("top")
        self.repo("group/deep")
        at_depth = self.registry.discover(max_depth=1)
        self.assertEqual([x["name"] for x in at_depth["candidates"]], ["top"])
        self.assertEqual(at_depth["coverage"]["max_depth"], 1)
        capped = self.registry.discover(max_candidates=1)
        self.assertEqual(len(capped["candidates"]), 1)
        self.assertIn("candidate_limit", capped["coverage"]["limited_by"])
        bounded = self.registry.discover(max_entries=1)
        self.assertEqual(bounded["coverage"]["examined_entries"], 1)
        self.assertIn("entry_limit", bounded["coverage"]["limited_by"])
        with self.assertRaises(BridgeError):
            self.registry.discover(max_depth=1000)

    def test_symlinks_and_outside_directories_are_not_discovered_or_registered(self):
        outside = self.root / "workspace-neighbor" / "secret"
        (outside / ".git").mkdir(parents=True)
        alias = self.allowed / "alias"
        alias.symlink_to(outside.parent, target_is_directory=True)
        fake = self.allowed / "fake-marker"
        fake.mkdir()
        (fake / ".git").symlink_to(outside / ".git", target_is_directory=True)
        self.assertEqual(self.registry.discover()["candidates"], [])
        self.assertEqual(self.registry.resolve("secret")["status"], "not_found")
        for path in (
            outside,
            alias / "secret",
            self.allowed / ".." / outside.parent.name / "secret",
        ):
            with self.subTest(path=path), self.assertRaises(BridgeError):
                self.registry.register_confirmed(path)
        self.assertFalse(self.store.exists())

    def test_registration_rechecks_path_and_does_not_leak_revoked_projects(self):
        old = self.repo("report")
        discovered = self.registry.resolve("report")
        self.assertEqual(discovered["status"], "needs_confirmation")
        target = self.root / "moved"
        old.rename(target)
        old.symlink_to(target, target_is_directory=True)
        with self.assertRaises(BridgeError):
            self.registry.register_confirmed(discovered["matches"][0]["root"])
        self.assertFalse(self.store.exists())

        live = self.repo("retained")
        entry = self.registry.register_confirmed(live)
        before = self.store.read_bytes()
        revoked = ProjectRegistry(self.store, allowed_roots=[])
        self.assertEqual(revoked.resolve(entry["project_id"])["matches"], [])
        self.assertEqual(revoked.discover()["candidates"], [])
        self.assertEqual(self.store.read_bytes(), before)

    def test_unavailable_scope_is_reported_not_claimed_unique(self):
        self.repo("report")
        other = self.root / "unavailable"
        registry = ProjectRegistry(self.store, allowed_roots=[self.allowed, other])
        result = registry.resolve("report")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(result["matches"]), 1)
        self.assertIn("unavailable_directory", result["coverage"]["limited_by"])
        self.assertFalse(self.store.exists())

    def test_concurrent_confirmations_reuse_one_identity(self):
        directory = self.repo("shared")
        script = (
            "import json,sys; from herdr_agent_bridge.project_registry import ProjectRegistry; "
            "sys.stdin.readline(); registry = ProjectRegistry(sys.argv[1], allowed_roots=[sys.argv[2]]); "
            "print(json.dumps(registry.register_confirmed(sys.argv[3])))"
        )
        command = [sys.executable, "-c", script, str(self.store), str(self.allowed), str(directory)]
        with (
            subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
            ) as first,
            subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
            ) as second,
        ):
            for process in (first, second):
                process.stdin.write("go\n")
                process.stdin.flush()
            results = [
                json.loads(process.communicate(timeout=15)[0]) for process in (first, second)
            ]
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.registry.resolve("shared")["matches"], [results[0]])
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
