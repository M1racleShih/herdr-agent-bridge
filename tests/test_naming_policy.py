"""The product naming gate accepts only explained, exact historical input lines."""

import importlib.util
import tempfile
import unittest
from pathlib import Path


def checker():
    path = Path(__file__).resolve().parents[1] / "tools/check_naming.py"
    spec = importlib.util.spec_from_file_location("product_naming", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NamingPolicyTests(unittest.TestCase):
    def test_unexplained_text_and_stage_filenames_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = "phase" + "9"
            (root / "README.md").write_text("new command: " + marker)
            (root / ("module_" + marker + ".py")).write_text("")
            issues = checker().scan(root, entries=[])
            self.assertEqual(len(issues), 2)

    def test_exception_is_exact_and_does_not_cover_new_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            line = "LEGACY = '" + "phase" + "9'"
            (root / "README.md").write_text(line + "\nnew usage: " + "phase" + "9\n")
            entries = [{"path": "README.md", "line": line, "reason": "bounded old input"}]
            issues = checker().scan(root, entries=entries)
            self.assertEqual(len(issues), 1)
            self.assertIn("README.md:2", issues[0])

    def test_stale_or_unexplained_exceptions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("current contract\n")
            for entry in (
                {"path": "README.md", "line": "absent", "reason": "old input"},
                {"path": "README.md", "line": "current contract", "reason": ""},
            ):
                self.assertTrue(checker().scan(root, entries=[entry]))
