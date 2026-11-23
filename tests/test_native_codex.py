"""Native Codex binding contracts with two disposable rollouts and fake hc."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_pi_bridge import FakeHC

from herdr_agent_bridge.agent_bridge import Bridge, BridgeError
from herdr_agent_bridge.codex_native import STATUS_LINE
from herdr_agent_bridge.development import collect

THREAD = "01a207ca-ad65-7c20-988e-4fc81ea9f653"
OTHER = "01a207ca-ad65-7c20-988e-4fc81ea9f654"


class NativeCodexTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.home = self.base / "codex"
        self.sessions = self.home / "sessions" / "2026" / "11" / "23"
        self.sessions.mkdir(parents=True)
        self.home_config = self.home / "config.toml"
        self.home_config.write_text("[features]\nhooks = true\n")
        self.project = self.base / "project"
        self.project.mkdir()
        self.hc = FakeHC()
        self.hc.terminal.update(
            cwd=str(self.project),
            pid=100,
            keep=True,
            status={"state": "idle", "app": "codex"},
        )
        self.hc.handlers["read"] = self.read
        self.thread = THREAD
        self.model = "GPT-6.2-Sol"
        self.tier = "Standard"

    def bridge(self, **kwargs):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home), "CODEX_THREAD_ID": OTHER}):
            bridge = Bridge.create(
                self.base / "run",
                self.project,
                model="gpt-6.2-sol",
                thinking="high",
                agent="codex",
                runtime="native",
                runner=self.hc,
                **kwargs,
            )
        self.hc.terminal["labels"] = {"bridge_run": bridge.data["session"]}
        bridge.launch()
        argv = self.hc.calls[-1][0]
        self.hc.terminal["argv"] = argv[argv.index("env") :]
        return bridge

    def read(self, args, env, timeout):
        self.assertEqual(args[args.index("--lines") + 1], "0")
        text = "  >_ OpenAI Codex\n\n› Ask Codex to do anything\n\n"
        if self.thread:
            suffix = " fast" if self.tier == "Fast" else ""
            text += f"  {self.thread} · {self.model} high{suffix} · {self.tier}\n"
        text += "  ← for agents · ? for shortcuts"
        return {"how": "screen", "complete": True, "text": text}

    def rollout(self, thread, entries=None):
        path = self.sessions / f"rollout-2026-11-23T17-44-12-{thread}.jsonl"
        mode = "a" if path.exists() else "w"
        if mode == "w":
            self.append(
                path,
                "session_meta",
                {"id": thread, "cwd": str(self.project), "history_mode": "paginated"},
            )
        for kind, payload in entries or []:
            self.append(path, kind, payload)
        return path

    @staticmethod
    def append(path, kind, payload):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": kind, "payload": payload}) + "\n")

    def turn(self, thread, task, *, turn_id=None, done=True):
        turn_id = turn_id or "native-" + task["id"]
        entries = [
            (
                "turn_context",
                {
                    "turn_id": turn_id,
                    "model": "gpt-6.2-sol",
                    "effort": "high",
                    "cwd": str(self.project),
                },
            ),
            (
                "event_msg",
                {
                    "type": "item_completed",
                    "thread_id": thread,
                    "turn_id": turn_id,
                    "item": {
                        "type": "UserMessage",
                        "id": "user-" + turn_id,
                        "content": [{"type": "text", "text": task["submitted_text"]}],
                    },
                },
            ),
        ]
        if done:
            answer = json.dumps({"task_id": task["id"], "kind": "result", "answer": {}})
            entries += [
                (
                    "event_msg",
                    {
                        "type": "item_completed",
                        "thread_id": thread,
                        "turn_id": turn_id,
                        "item": {
                            "type": "AgentMessage",
                            "id": "assistant-" + turn_id,
                            "phase": "final_answer",
                            "content": [{"type": "Text", "text": answer}],
                        },
                    },
                ),
                ("event_msg", {"type": "task_complete", "turn_id": turn_id}),
            ]
        return self.rollout(thread, entries)

    def settle(self):
        self.hc.terminal["turn"] += 1
        self.hc.terminal["status"] = {"state": "done", "app": "codex"}

    def test_missing_or_changed_live_thread_sends_nothing(self):
        bridge = self.bridge()
        command = self.hc.calls[0][0]
        self.assertIn(STATUS_LINE, command)
        self.assertIn("CODEX_HOME=" + str(self.home), command)
        self.assertNotIn("--dangerously-bypass-hook-trust", command)
        self.assertTrue(self.home_config.read_text().endswith("hooks = true\n"))
        self.thread = None
        with self.assertRaisesRegex(BridgeError, "codex_status_line_unavailable"):
            bridge.submit("one", "Do work")
        self.assertNotIn("codex_binding", bridge.data)
        self.thread = THREAD
        bridge.observe()
        self.assertEqual(Bridge(bridge.root).data["codex_binding"]["session_id"], THREAD)
        self.thread = OTHER
        with self.assertRaisesRegex(BridgeError, "codex_thread_changed"):
            bridge.submit("one", "Do work")
        self.thread = THREAD
        self.model = "GPT-6.1 Terra"
        with self.assertRaisesRegex(BridgeError, "codex_selection_mismatch"):
            bridge.submit("one", "Do work")
        self.assertEqual(self.hc.count("send"), 0)

    def test_startup_trust_prompt_is_reported_without_input(self):
        bridge = self.bridge()
        self.hc.terminal["status"] = None
        self.hc.handlers["read"] = lambda *_: {
            "how": "screen",
            "complete": True,
            "text": (
                f"  Folder access\n  {self.project}\n\n"
                "  Trust this folder? Codex can read, edit, and run files here.\n"
                "\n› 1. Trust and continue\n  2. Back to Agent Command Center\n"
            ),
        }
        bridge.observe()
        self.assertEqual(bridge.agent_selection()["reason"], "codex_folder_trust_required")
        with self.assertRaisesRegex(BridgeError, "codex_folder_trust_required"):
            bridge.submit("one", "Do work")
        self.assertEqual(self.hc.count("send"), 0)

    def test_known_unsupported_codex_effort_is_rejected_before_launch(self):
        cache = self.home / "models_cache.json"
        cache.write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": "gpt-6.2-sol",
                            "supported_reasoning_levels": [{"effort": "medium"}],
                        }
                    ]
                }
            )
        )
        with self.assertRaisesRegex(BridgeError, "does not support reasoning level high"):
            self.bridge()
        self.assertFalse((self.base / "run").exists())
        self.assertEqual(self.hc.count("run"), 0)

    def test_two_rounds_use_only_bound_rollout(self):
        bridge = self.bridge()
        first = bridge.submit("one", "Mention /fast verbatim")
        other = self.turn(OTHER, first)
        original = other.read_bytes()
        self.settle()
        self.assertNotEqual(bridge.observe("one")["observed"], "evidence_ready")
        self.turn(THREAD, first)
        self.assertEqual(bridge.observe("one")["observed"], "evidence_ready")
        second = bridge.submit("two", "Continue", parent="one")
        self.turn(THREAD, second)
        self.settle()
        for task in (first, second):
            observed = bridge.observe(task["id"])
            self.assertEqual(observed["observed"], "evidence_ready")
            self.assertEqual(observed["evidence"]["thread_id"], THREAD)
            self.assertEqual(observed["evidence"]["agent_turn_id"], "native-" + task["id"])
        self.assertEqual(self.hc.count("send"), 2)
        self.assertEqual(other.read_bytes(), original)

    def test_fast_result_requires_executed_native_tier_evidence(self):
        bridge = self.bridge()
        self.tier = "Fast"
        first = bridge.submit("one", "Require evidence of priority dispatch")
        path = self.rollout(THREAD)
        self.append(
            path,
            "event_msg",
            {
                "type": "thread_settings_applied",
                "thread_id": THREAD,
                "thread_settings": {"model": "gpt-6.2-sol", "service_tier": "priority"},
            },
        )
        self.turn(THREAD, first)
        self.settle()
        self.assertEqual(bridge.observe("one")["evidence"]["service_tier"], "fast")
        contents = path.read_text()
        path.write_text(contents.replace('"priority"', '"standard"'))
        result = bridge.observe("one")
        self.assertNotEqual(result["observed"], "evidence_ready")
        self.assertEqual(result["diagnostic"]["reason"], "codex_turn_fast_unconfirmed")

    def test_review_and_recovery_resume_bound_uuid_without_replay(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        bridge = self.bridge()
        first = bridge.submit("one", "Continue only after recovery")
        self.turn(THREAD, first, done=False)
        self.hc.terminal.update(pid=200, argv=["/bin/sh"], status=None)
        reviewed = bridge.recovery_status()
        self.turn(OTHER, first)
        self.assertEqual(bridge.recovery_status()["fingerprint"], reviewed["fingerprint"])

        def restart(args, env, timeout):
            command = shlex.split(args[-1])
            self.assertEqual(command[0], "exec")
            self.assertEqual(command[command.index("codex") + 1], "resume")
            self.assertEqual(command[-1], THREAD)
            self.assertNotIn(first["submitted_text"], command)
            self.hc.terminal.update(
                foreground={"pid": 200, "name": "codex", "argv": command[command.index("codex") :]},
                status={"state": "idle", "app": "codex"},
            )
            return {"started": True}

        self.hc.handlers["send"] = restart
        restored = bridge.recover(
            reviewed["fingerprint"], actor="human", review="Reviewed exact thread"
        )
        self.assertEqual(restored["state"], "restored")
        self.assertTrue(bridge.observe("one")["evidence"]["agent_lost"])
        self.hc.handlers.pop("send")
        second = bridge.submit("two", "Continue", parent="one")
        self.turn(THREAD, second)
        self.settle()
        self.assertEqual(bridge.observe("two")["observed"], "evidence_ready")
        self.assertEqual(bridge.observe("one")["observed"], "interrupted")

    def test_interrupt_requires_and_observes_the_exact_native_abort(self):
        bridge = self.bridge()
        first = bridge.submit("one", "Long operation")
        path = self.turn(THREAD, first, done=False)
        self.hc.terminal["status"] = {"state": "working", "app": "codex"}

        def interrupt(args, env, timeout):
            self.append(
                path,
                "event_msg",
                {"type": "turn_aborted", "turn_id": "native-one", "reason": "interrupted"},
            )
            self.settle()
            return {}

        self.hc.handlers["interrupt"] = interrupt
        self.thread = OTHER
        with self.assertRaisesRegex(BridgeError, "bound Codex thread"):
            bridge.interrupt("one", actor="human", note="stop")
        self.assertEqual(self.hc.count("interrupt"), 0)
        self.thread = THREAD
        result = bridge.interrupt("one", actor="human", note="stop")
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(bridge.observe("one")["observed"], "interrupted")

    def test_bound_session_changes_supersede_verification_and_operator_review(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / ".gitignore").write_text(".artifacts/\n")
        code = self.project / "value.txt"
        code.write_text("old\n")
        spec = {
            "task_id": "fix-value",
            "goal": "Update the value",
            "allowed_paths": ["value.txt"],
            "acceptance": ["value equals new"],
            "checks": [
                {
                    "name": "value",
                    "argv": [sys.executable, "-c", 'assert open("value.txt").read() == "new\\n"'],
                    "timeout": 10,
                }
            ],
        }
        bridge = self.bridge(development=spec)
        first = bridge.submit("one", "Fix value", development_task=spec["task_id"])
        bound = self.turn(THREAD, first)
        self.settle()
        self.assertEqual(bridge.observe("one")["observed"], "evidence_ready")
        code.write_text("new\n")
        snapshots = bridge.reconciliation()
        self.turn(OTHER, first)
        self.assertEqual(snapshots, bridge.reconciliation())

        ticket = bridge.begin_verification()
        result = collect(self.project, ticket.contract)
        self.assertTrue(result["passed"])
        self.append(bound, "event_msg", {"type": "token_count"})
        self.assertEqual(
            bridge.finish_verification(ticket, result)["reason"], "verification_superseded"
        )

        bridge.takeover("human")
        reviewed = bridge.reconciliation()["fingerprint"]
        self.thread = OTHER
        with self.assertRaisesRegex(BridgeError, "bound Codex history"):
            bridge.resume(reviewed, "I checked this run")
        self.thread = THREAD
        self.append(bound, "event_msg", {"type": "token_count"})
        with self.assertRaisesRegex(BridgeError, "session changed"):
            bridge.resume(reviewed, "I checked this run")
        current = bridge.reconciliation()["fingerprint"]
        self.assertEqual(
            bridge.resume(current, "I checked the changed rollout")["control"], "automation"
        )


if __name__ == "__main__":
    unittest.main()
