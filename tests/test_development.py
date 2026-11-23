"""Development contracts against disposable local git trees; no agents/network."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_pi_bridge import FakeHC, assistant_message, session_header, user_message

from herdr_agent_bridge import development
from herdr_agent_bridge.agent_bridge import Bridge, BridgeError
from herdr_agent_bridge.grok_bridge import Manager


class DevelopmentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / "code.py").write_text("value = 1\n")
        (self.project / "other.txt").write_text("existing work\n")
        self.spec = {
            "task_id": "fix-value",
            "goal": "Set value to two",
            "allowed_paths": ["code.py"],
            "acceptance": ["value equals two"],
            "checks": [
                {
                    "name": "value",
                    "argv": [sys.executable, "-c", "from code import value; assert value == 2"],
                    "timeout": 10,
                }
            ],
        }

    @staticmethod
    def sdk_selection_ready(bridge):
        # Fake hc has no Pi process; represent its SDK's confirmed selection.
        (bridge.root / "pi-selection.json").write_text(
            json.dumps(
                {
                    "state": "ready",
                    "model": bridge.data["model"],
                    "thinking": bridge.data["thinking"],
                    "session_id": "pi-session-01",
                }
            )
        )

    def native_history(self, bridge, task_id, text, answer):
        provider, model_id = bridge.data["model"].split("/", 1)
        model = {
            "type": "model_change",
            "id": "m1",
            "parentId": None,
            "timestamp": "2026-01-02T03:04:05.000Z",
            "provider": provider,
            "modelId": model_id,
        }
        thinking = {
            "type": "thinking_level_change",
            "id": "t1",
            "parentId": "m1",
            "timestamp": "2026-01-02T03:04:05.000Z",
            "thinkingLevel": bridge.data["thinking"],
        }
        return [
            session_header(self.project),
            model,
            thinking,
            user_message("u1", "t1", text),
            assistant_message("a1", "u1", task_id, answer, provider=provider, model=model_id),
        ]

    def test_final_evidence_requires_checks_and_preserves_unrelated_work(self):
        contract = development.prepare(self.project, self.spec)
        before = development.collect(self.project, contract)
        self.assertFalse(before["passed"])
        (self.project / "code.py").write_text("value = 2\n")
        after = development.collect(self.project, contract)
        self.assertTrue(after["passed"])
        self.assertIn("+value = 2", after["diff"])
        self.assertEqual(after["changed_paths"], ["code.py"])
        (self.project / "other.txt").write_text("lost previous work\n")
        self.assertFalse(development.collect(self.project, contract)["passed"])

    def test_development_prompt_displays_unicode_contract_as_readable_json(self):
        source = self.project / ".artifacts" / "source"
        source.mkdir(parents=True)
        (source / "settings.json").write_text("{}")
        (source / "auth.json").write_text("{}")
        spec = {
            **self.spec,
            "goal": "修复餐次选择",
            "allowed_paths": ["代码.py"],
            "acceptance": ["点击“＋”时复制当前餐次", r'保留字面量 \u4e2d 和 "引号"'],
            "checks": [
                {
                    "name": "check",
                    "argv": [sys.executable, "-c", 'print("检查完成")'],
                    "timeout": 10,
                }
            ],
        }
        hc = FakeHC()
        bridge = Bridge.create(
            self.project / ".artifacts" / "run",
            self.project,
            source_home=source,
            runner=hc,
            agent="pi",
            development=spec,
        )
        self.addCleanup(bridge.close)
        bridge.launch()
        self.sdk_selection_ready(bridge)
        task = bridge.submit("round-unicode", "请修复选择器。", development_task=spec["task_id"])
        for label, value, readable in [
            ("Allowed files", spec["allowed_paths"], "代码.py"),
            ("Acceptance", spec["acceptance"], "复制当前餐次"),
            ("Checks", spec["checks"], "检查完成"),
        ]:
            with self.subTest(field=label):
                text = task["submitted_text"].split(label + ": ", 1)[1].splitlines()[0]
                self.assertEqual(json.loads(text), value)
                self.assertIn(readable, text)
        self.assertEqual(task["submitted_text"], hc.calls[-1][0][-1])

    def test_check_that_leaves_a_background_descendant_cannot_pass(self):
        pid_file = self.project / ".artifacts" / "child.pid"
        pid_file.parent.mkdir()
        script = (
            "import pathlib,subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import signal; signal.pause()'],close_fds=False); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"
        )
        contract = development.prepare(
            self.project,
            {
                **self.spec,
                "checks": [{"name": "fork", "argv": [sys.executable, "-c", script], "timeout": 2}],
            },
        )
        (self.project / "code.py").write_text("value = 2\n")
        try:
            result = development.collect(self.project, contract)
            self.assertFalse(result["passed"])
            self.assertEqual(result["reason"], "check_left_descendants")
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_development_input_requires_original_task_and_operator_ownership(self):
        source = self.project / ".artifacts" / "source"
        source.mkdir(parents=True)
        (source / "settings.json").write_text(
            json.dumps({"defaultProvider": "fixture", "defaultModel": "fixture"})
        )
        (source / "auth.json").write_text("{}")
        hc = FakeHC()
        bridge = Bridge.create(
            self.project / ".artifacts" / "run",
            self.project,
            source_home=source,
            runner=hc,
            agent="pi",
            development=self.spec,
        )
        self.addCleanup(bridge.close)
        bridge.launch()
        self.sdk_selection_ready(bridge)
        with self.assertRaises(BridgeError):
            Bridge.create(
                self.project / ".artifacts" / "second-run",
                self.project,
                source_home=source,
                runner=hc,
                agent="pi",
                development=self.spec,
            )
        with self.assertRaises(BridgeError):
            bridge.submit("unrelated", "second task")
        with self.assertRaises(BridgeError):
            bridge.submit("round-1", "fix", development_task="other")
        bridge.takeover("operator-proxy")
        with self.assertRaises(BridgeError):
            bridge.submit("round-1", "fix", development_task="fix-value")
        self.assertEqual(hc.count("send"), 0)
        turn = bridge.submit(
            "round-1",
            "Inspect only; await my next instruction",
            development_task="fix-value",
            operator=True,
        )
        self.assertIn("Development task fix-value", turn["submitted_text"])
        self.assertEqual(hc.count("send"), 0)
        with self.assertRaises(BridgeError):
            bridge.resume("not-the-current-fingerprint", "reviewed")
        path = Path(bridge.data["scratch"]) / "pi" / "sessions" / "session.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = self.native_history(
            bridge, "round-1", turn["submitted_text"], {"summary": "operator acknowledged"}
        )
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        hc.terminal["turn"] = 1
        hc.terminal["status"]["state"] = "done"
        bridge.observe()
        reviewed = bridge.reconciliation()["fingerprint"]
        (self.project / "other.txt").write_text("operator changed this after review\n")
        with self.assertRaises(BridgeError):
            bridge.resume(reviewed, "stale review")
        bridge.resume(
            bridge.reconciliation()["fingerprint"], "reviewed original task and operator input"
        )
        bridge.submit(
            "round-2", "continue same task", parent="round-1", development_task="fix-value"
        )
        self.assertEqual(hc.count("send"), 1)

    def test_manager_verifies_final_code_but_requires_explicit_acceptance(self):
        source = self.project / ".artifacts" / "source"
        source.mkdir(parents=True)
        (source / "settings.json").write_text(
            json.dumps({"defaultProvider": "fixture", "defaultModel": "fixture"})
        )
        (source / "auth.json").write_text("{}")
        hc = FakeHC()
        manager = Manager.create(
            self.project / ".artifacts" / "runs" / "run",
            self.project,
            {"bot_id": "fixture", "conversation_id": "fixture"},
            source_home=source,
            runner=hc,
            agent="pi",
            development=self.spec,
        )
        self.addCleanup(manager.bridge().close)
        self.sdk_selection_ready(manager.bridge())
        manager.submit("round-1", "fix", development_task="fix-value")
        bridge = manager.bridge()
        task = bridge.task("round-1")
        path = Path(bridge.data["scratch"]) / "pi" / "sessions" / "session.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = self.native_history(
            bridge, "round-1", task["submitted_text"], {"summary": "fixed"}
        )
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        hc.terminal["turn"] = 1
        hc.terminal["status"]["state"] = "done"
        manager.observe()
        (self.project / "code.py").write_text("value = 2\n")
        result = manager.verify_development()
        self.assertTrue(result["passed"])
        self.assertEqual(manager.status()["development"]["state"], "active")
        (self.project / "code.py").write_text("value = 3\n")
        with self.assertRaises(BridgeError):
            manager.decide_development("accept", "operator", "reviewed")
        manager.decide_development("terminate", "operator", "changed after verification")
        with self.assertRaises(BridgeError):
            manager.submit("round-2", "another", parent="round-1", development_task="fix-value")

    def test_contract_rejects_symlinks_traversal_and_missing_acceptance(self):
        (self.project / "link.py").symlink_to(self.project / "code.py")
        for path in ("link.py", "../outside.py", "/tmp/outside.py", "a/../code.py", ".git/config"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                development.prepare(self.project, {**self.spec, "allowed_paths": [path]})
        with self.assertRaises(ValueError):
            development.prepare(self.project, {**self.spec, "acceptance": []})

    def test_invalid_agent_does_not_reserve_a_development_task(self):
        with self.assertRaises(BridgeError):
            Bridge.create(
                self.project / ".artifacts" / "invalid",
                self.project,
                agent="codex",
                development=self.spec,
            )
        self.assertFalse((self.project / ".artifacts" / "development-active.json").exists())


if __name__ == "__main__":
    unittest.main()
