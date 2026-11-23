"""Native watcher contracts using disposable journals and a fake hc only."""

import copy
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_agent_bridge.agent_bridge import Bridge, BridgeError, HcError, save
from herdr_agent_bridge.grok_bridge import Manager

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

OWNER = {"bot_id": "private-bot", "conversation_id": "private-conversation"}


class FakeHC:
    def __init__(self):
        self.calls = []
        self.terminals = {}
        self.on_run = None
        self.on_kill = None

    def __call__(self, args, env, timeout):
        args = list(args)
        self.calls.append((args, dict(env), timeout))
        command = args[0]
        if command == "run":
            name = args[args.index("--name") + 1]
            label, value = args[args.index("--label") + 1].split("=", 1)
            address = "t" + str(len([c for c, _, _ in self.calls if c[0] == "run"]))
            terminal = {
                "address": address,
                "name": name,
                "labels": {label: value},
                "state": "live",
                "status": {"state": "idle"},
                "created_ms": 0,
                "turn": 0,
            }
            if self.on_run:
                self.on_run(args, env, timeout, terminal)
            self.terminals[address] = terminal
            return {"address": address}
        if command == "get":
            target = args[-1]
            for terminal in self.terminals.values():
                if target in (terminal["address"], terminal["name"]):
                    return copy.deepcopy(terminal)
            raise HcError({"stderr": json.dumps({"error": {"kind": "not_found"}})})
        if command == "kill":
            if self.on_kill:
                self.on_kill(args, env, timeout)
            self.terminals[args[1]]["state"] = "finished"
            return {}
        if command == "rm":
            del self.terminals[args[1]]
            return {}
        if command == "wait":
            return {"reason": "timeout"}
        if command == "send":
            return {"started": True}
        if command == "read":
            return {"text": "fixture"}
        raise AssertionError("unexpected hc call: " + repr(args))

    def commands(self, name):
        return [args for args, _, _ in self.calls if args[0] == name]


class NativeMonitorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hab-native-monitor-")
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name) / "project"
        self.project.mkdir()
        self.root = self.project / ".artifacts" / "runs" / "run"
        self.hc = FakeHC()
        self.config = Path(temp.name) / "webhook.json"
        save(self.config, {**OWNER, "url": "https://example.invalid/routine", "key": "SECRET-KEY"})
        self.sent = []
        self.manager = Manager.create(
            self.root,
            self.project,
            OWNER,
            agent="pi",
            runtime="native",
            webhook_config=self.config,
            runner=self.hc,
            sender=lambda *args: self.sent.append(args) or 200,
        )

    def watcher(self):
        return json.loads(self.manager.path.read_text())["monitor"]

    def test_native_watch_subscribes_and_refreshes_state_without_wait_polling(self):
        closed, subscribed = [], []
        owner = self

        class Events:
            stats = {"connections": 1, "events": 0, "state": "connected", "pid": None}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                closed.append(True)

            def read(self, timeout):
                owner.hc.terminals["t1"]["status"] = {"state": "blocked"}
                self.stats["events"] += 1
                return True

        def source(terminal, env):
            subscribed.append(terminal)
            return Events()

        result = self.manager.watch(2, event_source=source)
        self.assertEqual(subscribed, ["t1"])
        self.assertEqual(result["watch_status"], "outcome")
        self.assertEqual(result["subscription"]["events"], 1)
        self.assertEqual(closed, [True])
        self.assertFalse(self.hc.commands("wait"))
        self.assertEqual(len(self.sent), 1)

    def test_transient_core_unavailability_reconnects_before_observing_the_original_terminal(self):
        original = self.manager.runner
        available = [False]

        def runner(args, env, timeout):
            if args[0] == "get" and not available[0]:
                raise OSError("temporary core connection loss")
            return original(args, env, timeout)

        self.manager.runner = runner
        owner = self

        class Events:
            stats = {"connections": 1, "events": 0}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, timeout):
                available[0] = True
                owner.hc.terminals["t1"]["status"] = {"state": "blocked"}
                return True

        result = self.manager.watch(2, event_source=lambda *_: Events())
        self.assertEqual(result["watch_status"], "outcome")
        self.assertEqual(len(self.sent), 1)

    def test_native_launch_records_identity_first_and_keeps_environment(self):
        bridge_env = self.manager.bridge().data["native_env"]

        def inspect(args, env, timeout, terminal):
            saved = self.watcher()
            self.assertEqual(saved["state"], "launch_unknown")
            self.assertIsNone(saved["address"])
            self.assertEqual(saved["name"], terminal["name"])
            self.assertEqual(saved["token"], terminal["labels"]["bridge_monitor"])
            self.assertIn("--no-parent", args)
            self.assertIn("--no-notify", args)
            self.assertIn("--bg", args)
            self.assertLessEqual(len(saved["name"]), 32)
            for key, value in bridge_env.items():
                if value is not None:
                    self.assertEqual(env[key], value)
                    self.assertIn(key + "=" + value, args)
            self.assertNotIn(OWNER["bot_id"], args)
            self.assertNotIn(OWNER["conversation_id"], args)
            self.assertNotIn("SECRET-KEY", " ".join(args))
            self.assertIn("herdr_agent_bridge.managed_watch", args)

        self.hc.on_run = inspect
        monitor = self.manager.monitor(5)["monitor"]
        self.assertEqual(
            (monitor["backend"], monitor["state"], monitor["address"]), ("hc", "starting", "t2")
        )
        self.assertEqual(self.watcher()["address"], "t2")
        self.assertEqual(self.manager.status()["monitor"]["address"], "t2")

    def test_unknown_launch_is_not_retried_and_close_reconciles_by_name_and_label(self):
        def lost(args, env, timeout, terminal):
            self.hc.terminals[terminal["address"]] = terminal
            raise subprocess.TimeoutExpired("hc run", timeout)

        self.hc.on_run = lost
        with self.assertRaises(subprocess.TimeoutExpired):
            self.manager.monitor(5)
        with self.assertRaisesRegex(BridgeError, "unresolved"):
            self.manager.monitor(5)
        self.assertEqual(len(self.hc.commands("run")), 2)  # One agent and one monitor.
        self.assertIsNone(self.watcher()["address"])
        result = self.manager.close()
        self.assertTrue(result["monitor_cleanup"][0]["passed"])
        self.assertEqual(self.watcher()["address"], "t2")
        self.assertNotIn("t2", self.hc.terminals)
        self.assertFalse(self.hc.commands("server"))

    def test_absence_after_uncertain_launch_stays_unresolved(self):
        self.hc.on_run = lambda args, env, timeout, terminal: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("hc run", timeout)
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            self.manager.monitor(5)
        result = self.manager.close()
        self.assertFalse(result["monitor_cleanup"][0]["passed"])
        self.assertEqual(self.watcher()["cleanup"]["reason"], "launch outcome not yet observed")
        self.assertFalse(any(args[1] == "t2" for args in self.hc.commands("kill")))

    def test_cleanup_refuses_changed_label_name_and_address(self):
        for field in ("label", "name", "address"):
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory(prefix="hab-monitor-identity-") as temp,
            ):
                project = Path(temp) / "project"
                project.mkdir()
                hc = FakeHC()
                manager = Manager.create(
                    project / ".artifacts" / "runs" / "run",
                    project,
                    OWNER,
                    agent="pi",
                    runtime="native",
                    runner=hc,
                )
                monitor = manager.monitor(5)["monitor"]
                terminal = hc.terminals[monitor["address"]]
                original = copy.deepcopy(terminal)
                if field == "label":
                    terminal["labels"]["bridge_monitor"] = "other"
                elif field == "name":
                    terminal["name"] = "somebody-else"
                else:
                    # Return a different address while still resolving the target.
                    real = hc.__call__
                    with patch.object(Bridge, "hc", autospec=True) as call:

                        def changed(bridge, *args, **kwargs):
                            value = real(args, bridge.env(), kwargs.get("timeout", 20))
                            if args[0] == "get" and args[-1] == monitor["address"]:
                                value["address"] = "t-other"
                            return value

                        call.side_effect = changed
                        result = manager.close()
                if field != "address":
                    result = manager.close()
                self.assertFalse(result["monitor_cleanup"][0]["passed"])
                self.assertFalse(any(args[1] == monitor["address"] for args in hc.commands("kill")))
                self.assertFalse(any(args[1] == monitor["address"] for args in hc.commands("rm")))
                self.assertFalse(hc.commands("server"))
                hc.terminals[monitor["address"]] = original
                self.assertTrue(manager.close()["monitor_cleanup"][0]["passed"])

    def test_cleanup_rechecks_identity_after_kill_before_removing(self):
        monitor = self.manager.monitor(5)["monitor"]

        def change_label(args, env, timeout):
            if args[1] == monitor["address"]:
                self.hc.terminals[monitor["address"]]["labels"]["bridge_monitor"] = "other"

        self.hc.on_kill = change_label
        result = self.manager.close()
        self.assertFalse(result["closed"])
        self.assertFalse(result["monitor_cleanup"][0]["passed"])
        self.assertFalse(any(args[1] == monitor["address"] for args in self.hc.commands("rm")))
        self.hc.terminals[monitor["address"]]["labels"]["bridge_monitor"] = monitor["token"]
        self.assertTrue(self.manager.close()["closed"])

    def test_cleanup_never_holds_manager_lock_and_does_not_replay_unknown_kill(self):
        monitor = self.manager.monitor(5)["monitor"]
        failures = []

        def lost(args, env, timeout):
            if args[1] == monitor["address"]:
                with (self.root / "manager.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                failures.append("lost")
                raise subprocess.TimeoutExpired("hc kill", timeout)

        self.hc.on_kill = lost
        first = self.manager.close()
        self.assertFalse(first["monitor_cleanup"][0]["passed"])
        self.assertFalse(first["closed"])
        self.assertFalse(first["input_ready"])
        with self.assertRaisesRegex(BridgeError, "close is in progress"):
            self.manager.submit("other", "never submitted")
        self.assertEqual(failures, ["lost"])
        second = self.manager.close()
        self.assertFalse(second["monitor_cleanup"][0]["passed"])
        self.assertEqual(failures, ["lost"])
        self.hc.terminals[monitor["address"]]["state"] = "finished"
        final = self.manager.close()
        self.assertTrue(final["monitor_cleanup"][0]["passed"])
        self.assertTrue(final["closed"])
        self.assertEqual(failures, ["lost"])
        self.assertFalse(self.hc.commands("server"))

    def test_outcome_is_notified_once_without_submitting_and_old_watchers_are_cleaned(self):
        bridge = self.manager.bridge()
        bridge.data["tasks"] = [
            {
                "id": "existing",
                "parent": None,
                "delivery": "received",
                "observed": "evidence_ready",
                "verified_success": False,
                "evidence": {"agent_completed": True, "result": {"kind": "result"}},
            }
        ]
        bridge.persist()
        first = self.manager.monitor(5)["monitor"]
        with (
            patch.object(Bridge, "observe"),
            patch.object(Bridge, "pi_selection", return_value=None),
        ):
            outcome = self.manager.watch(5, first["token"])
            second = self.manager.monitor(5)["monitor"]
            duplicate = self.manager.watch(5, second["token"])
        self.assertEqual(outcome["event_id"], duplicate["event_id"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.manager.outbox().get(outcome["event_id"])["attempts"]), 1)
        self.assertFalse(self.hc.commands("send"))
        self.assertTrue(all(item["passed"] for item in self.manager.close()["monitor_cleanup"]))
        self.assertNotIn(first["address"], self.hc.terminals)
        self.assertNotIn(second["address"], self.hc.terminals)

    def test_real_bootstrap_reads_private_owner_and_writes_log_using_fake_hc(self):
        # Replace the stored PATH before launch so the child invokes a disposable
        # executable. The helper has no owner or webhook credentials in argv.
        self.manager.configure(self.config)
        with self.manager.locked():
            self.manager.data["webhook_config"] = None  # No network in this test.
            save(self.manager.path, self.manager.data)
        binary = self.project / "bin"
        binary.mkdir()
        terminal = self.hc.terminals["t1"]
        executable = binary / "hc"
        executable.write_text(
            "#!" + sys.executable + "\nimport json,sys\n"
            'if sys.argv[1] == "get": print(' + repr(json.dumps(terminal)) + ")\n"
            'elif sys.argv[1] == "wait": print(json.dumps({"reason":"timeout"}))\n'
            'elif sys.argv[1] == "read": print(json.dumps({"text":"fixture"}))\n'
            "else: raise SystemExit(99)\n"
        )
        executable.chmod(0o700)
        bridge = self.manager.bridge()
        bridge.data["native_env"]["HOME"] = str(self.project / "different-agent-home")
        bridge.data["native_env"]["PATH"] = str(binary) + os.pathsep + os.environ.get("PATH", "")
        bridge.persist()
        save(
            bridge.root / "pi-selection.json",
            {
                "state": "ready",
                "model": "fixture/model",
                "thinking": "high",
                "session_id": "fixture-session",
            },
        )
        monitor = self.manager.monitor(5)["monitor"]
        command = self.hc.commands("run")[-1]
        arguments = command[command.index("herdr_agent_bridge.managed_watch") + 1 :]
        shadow = self.project / "herdr_agent_bridge"
        shadow.mkdir()
        marker = self.project / "shadow-executed"
        (shadow / "__init__.py").write_text(
            "from pathlib import Path; Path("
            + repr(str(marker))
            + ").touch(); raise RuntimeError('untrusted shadow imported')"
        )
        child = subprocess.run(
            command[command.index("env") :],
            cwd=self.project,
            env={
                **os.environ,
                "PYTHONPATH": str(self.project),
                "PYTHONNOUSERSITE": "1",
                "PYTHONHOME": str(self.project / "bogus-python"),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(
            (child.returncode, child.stdout, child.stderr),
            (0, "", ""),
            (self.root / "monitor.log").read_text(),
        )
        self.assertFalse(marker.exists())
        self.assertIn("-P", command)
        self.assertIn("PYTHONPATH", command)
        self.assertIn("PYTHONHOME", command)
        self.assertEqual(self.watcher()["state"], "finished")
        self.assertEqual(self.watcher()["reason"], "outcome")
        self.assertEqual((self.root / "monitor.log").stat().st_mode & 0o777, 0o600)
        self.assertNotIn(OWNER["bot_id"], arguments)
        self.assertNotIn("SECRET-KEY", " ".join(arguments))
        self.assertEqual(monitor["backend"], "hc")


if __name__ == "__main__":
    unittest.main()
