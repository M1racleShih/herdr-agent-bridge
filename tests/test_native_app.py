"""App association contracts using fake hc and fake window responses only."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from herdr_agent_bridge.agent_bridge import BridgeError, save
from herdr_agent_bridge.app_session import AppSession


def mapping(space=None, pane="pane-owned", address="t-owned"):
    spaces = [
        {"id": "requested", "machine": None, "active": False, "tabs": []},
        {
            "id": "other",
            "machine": None,
            "active": True,
            "tabs": [
                {
                    "id": "other-tab",
                    "terminals": [{"terminal": "t-unrelated", "pane": "other-pane"}],
                }
            ],
        },
        {"id": "remote", "machine": "remote-machine", "tabs": []},
    ]
    if space:
        target = next(item for item in spaces if item["id"] == space)
        target["tabs"].append(
            {"id": "owned-tab", "terminals": [{"terminal": address, "pane": pane}]}
        )
    return {"spaces": spaces, "recent": []}


class FakeHC:
    def __init__(self, app):
        self.app = app
        self.calls = []
        self.terminal = {
            "address": "t-owned",
            "state": "live",
            "labels": {"bridge_run": "bridge-session"},
        }
        self.fail_label = False

    def __call__(self, args, env, timeout):
        self.calls.append(list(args))
        if args[0] == "get":
            return copy.deepcopy(self.terminal)
        if args[0] == "label":
            saved = json.loads(self.app.path.read_text())
            if saved.get("binding") != "unknown" or saved.get("space") != "requested":
                raise AssertionError("binding intent was not durable before label")
            key, value = args[2].split("=", 1)
            self.terminal["labels"][key] = value
            if self.fail_label:
                raise subprocess.TimeoutExpired("hc label", timeout)
            return {}
        if args[0] == "run":
            return {"address": "app-terminal"}
        if args[0] == "wait":
            return {"ended": True}
        if args[0] == "rm":
            return {}
        raise AssertionError(f"unexpected hc call: {args}")


class NativeAppTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hab-native-app-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.native_env = {
            "HOME": "/normal/home",
            "XDG_CONFIG_HOME": "/normal/config",
            "XDG_STATE_HOME": "/normal/state",
            "XDG_RUNTIME_DIR": "/normal/run",
            "XDG_CACHE_HOME": "/normal/cache",
            "HERDR_APP_HOME": "/normal/app",
            "HC_SESSION": None,
            "CODEX_HOME": None,
            "PI_CODING_AGENT_DIR": None,
        }
        self.journal = {
            "runtime": "native",
            "session": "bridge-session",
            "native_env": self.native_env,
            "scratch": str(self.root / "scratch"),
            "project": str(self.root),
            "terminal": "bridge-session",
            "address": "t-owned",
            "agent": "pi",
            "launch": "submitted",
            "closed": False,
        }
        save(self.root / "bridge" / "journal.json", self.journal)
        self.app = AppSession(self.root)
        self.hc = FakeHC(self.app)
        self.app.bridge.runner = self.hc

    def attach(self):
        with patch.object(self.app, "command", return_value=mapping()) as command:
            self.app.start()
        command.assert_called_once_with("ls")

    def visible_listing(self, *args):
        self.assertEqual(args, ("ls",))
        space = self.hc.terminal["labels"].get("herdr.space")
        return mapping(space if space in ("requested", "other", "remote") else None)

    def test_native_start_uses_captured_environment_and_never_launches(self):
        response = SimpleNamespace(returncode=0, stdout=json.dumps(mapping()))
        with patch.dict(
            os.environ,
            {
                "HOME": "/changed",
                "XDG_CONFIG_HOME": "/changed",
                "HERDR_APP_HOME": "/changed",
                "HC_SESSION": "changed",
                "HERDR_APP_NO_SOUND": "normal-sound",
            },
            clear=True,
        ):
            with patch(
                "herdr_agent_bridge.app_session.subprocess.run", return_value=response
            ) as run:
                result = self.app.start()
        self.assertEqual(result["launch"], "attached")
        self.assertEqual(self.hc.calls, [])
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["herdr-app", "ls"])
        for key in (
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "XDG_CACHE_HOME",
            "HERDR_APP_HOME",
        ):
            self.assertEqual(kwargs["env"][key], self.native_env[key])
        self.assertNotIn("HC_SESSION", kwargs["env"])
        self.assertEqual(kwargs["env"]["HERDR_APP_NO_SOUND"], "normal-sound")

    def test_start_refuses_display_override_and_missing_running_window(self):
        with self.assertRaises(BridgeError):
            self.app.start(display=":99")
        self.assertFalse(self.app.path.exists())
        response = SimpleNamespace(returncode=1, stdout="")
        with patch("herdr_agent_bridge.app_session.subprocess.run", return_value=response):
            with self.assertRaises(BridgeError):
                self.app.start()
        self.assertFalse(self.app.path.exists())
        self.assertEqual(self.hc.calls, [])

    def test_closed_bridge_cannot_open_or_bind_native_app(self):
        self.app.bridge.data["closed"] = True
        with patch.object(self.app, "command", return_value=mapping()) as command:
            with self.assertRaises(BridgeError):
                self.app.start()
            command.assert_not_called()
            self.app.bridge.data["closed"] = False
            self.app.start()
            self.app.bridge.data["closed"] = True
            with self.assertRaises(BridgeError):
                self.app.bind("requested")
        self.assertEqual(self.hc.calls, [])

    def test_space_without_machine_identity_is_not_assumed_local(self):
        self.attach()
        unknown = mapping()
        del unknown["spaces"][0]["machine"]
        with patch.object(self.app, "command", return_value=unknown):
            with self.assertRaises(BridgeError):
                self.app.bind("requested")
        self.assertFalse(any(call[0] == "label" for call in self.hc.calls))

    def test_bind_targets_exact_space_and_records_only_owned_pane(self):
        self.attach()
        with patch.object(self.app, "command", side_effect=self.visible_listing) as command:
            result = self.app.bind("requested")
        self.assertEqual(result["binding"], "bound")
        self.assertEqual(
            result["visible"],
            [
                {
                    "space": "requested",
                    "tab": "owned-tab",
                    "pane": "pane-owned",
                    "terminal": "t-owned",
                }
            ],
        )
        self.assertEqual(
            self.hc.calls,
            [
                ["get", "--json", "t-owned"],
                ["label", "t-owned", "herdr.space=requested"],
                ["get", "--json", "t-owned"],
            ],
        )
        self.assertTrue(all(call.args == ("ls",) for call in command.call_args_list))
        self.assertNotIn("t-unrelated", self.app.path.read_text())

    def test_existing_correct_label_is_observed_without_rewriting_it(self):
        self.attach()
        self.hc.terminal["labels"]["herdr.space"] = "requested"
        with patch.object(self.app, "command", side_effect=self.visible_listing):
            self.assertEqual(self.app.bind("requested")["binding"], "bound")
        self.assertFalse(any(call[0] == "label" for call in self.hc.calls))

    def test_missing_wrong_or_remote_space_does_not_change_the_terminal(self):
        for space in (None, "", "missing", "remote"):
            with self.subTest(space=space):
                self.attach() if not self.app.data else None
                with patch.object(self.app, "command", return_value=mapping()):
                    with self.assertRaises(BridgeError):
                        self.app.bind(space)
                self.assertEqual(self.app.data["binding"], "not_attempted")
                self.assertFalse(any(call[0] == "label" for call in self.hc.calls))

    def test_unowned_address_and_conflicting_labels_never_attach(self):
        self.attach()
        for change in ("wrong-address", "wrong-owner", "other-space", "orphan-tab", "finished"):
            with self.subTest(change=change):
                self.hc.terminal = {
                    "address": "t-owned",
                    "state": "live",
                    "labels": {"bridge_run": "bridge-session"},
                }
                if change == "wrong-address":
                    self.hc.terminal["address"] = "t-unrelated"
                elif change == "wrong-owner":
                    self.hc.terminal["labels"]["bridge_run"] = "other-session"
                elif change == "other-space":
                    self.hc.terminal["labels"]["herdr.space"] = "other"
                elif change == "orphan-tab":
                    self.hc.terminal["labels"]["herdr.tab"] = "other-tab"
                else:
                    self.hc.terminal["state"] = "finished"
                with patch.object(self.app, "command", return_value=mapping()):
                    with self.assertRaises(BridgeError):
                        self.app.bind("requested")
                self.assertEqual(self.app.data["binding"], "not_attempted")
                self.assertFalse(any(call[0] == "label" for call in self.hc.calls))

    def test_wrong_existing_mapping_is_not_moved(self):
        self.attach()
        with patch.object(self.app, "command", return_value=mapping("other")):
            with self.assertRaises(BridgeError):
                self.app.bind("requested")
        self.assertEqual(self.app.data["binding"], "not_attempted")
        self.assertFalse(any(call[0] == "label" for call in self.hc.calls))

    def test_lost_label_reply_is_not_replayed(self):
        self.attach()
        self.hc.fail_label = True
        with patch.object(self.app, "command", return_value=mapping()):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.app.bind("requested")
            self.assertEqual(json.loads(self.app.path.read_text())["binding"], "unknown")
            with self.assertRaises(BridgeError):
                AppSession(self.root).bind("requested")
        self.assertEqual(sum(call[0] == "label" for call in self.hc.calls), 1)

    def test_wrong_resulting_space_or_missing_pane_never_claims_bound(self):
        for resulting in (mapping("other"), mapping("requested", pane=None)):
            with self.subTest(resulting=resulting):
                self.app.data = {}
                self.app.path.unlink(missing_ok=True)
                self.hc.terminal["labels"] = {"bridge_run": "bridge-session"}
                self.attach()
                with patch.object(self.app, "command", side_effect=[mapping(), resulting]):
                    with self.assertRaises(BridgeError):
                        self.app.bind("requested")
                self.assertEqual(json.loads(self.app.path.read_text())["binding"], "unknown")

    def test_timeout_keeps_uncertain_binding_and_cannot_replay(self):
        self.attach()
        with patch.object(self.app, "command", return_value=mapping()):
            with patch("herdr_agent_bridge.app_session.time.monotonic", side_effect=[0, 16]):
                with self.assertRaises(BridgeError):
                    self.app.bind("requested")
            with self.assertRaises(BridgeError):
                self.app.bind("requested")
        self.assertEqual(json.loads(self.app.path.read_text())["binding"], "unknown")
        self.assertEqual(sum(call[0] == "label" for call in self.hc.calls), 1)

    def test_close_only_marks_association_and_status_does_not_list_other_terminals(self):
        self.attach()
        with patch.object(self.app, "command", side_effect=self.visible_listing):
            self.app.bind("requested")
            result = self.app.status()
        self.assertEqual(result["visible"][0]["terminal"], "t-owned")
        self.assertNotIn("t-unrelated", json.dumps(result))
        self.hc.calls.clear()
        with patch.object(self.app, "command", side_effect=AssertionError("unexpected App action")):
            self.assertTrue(self.app.close()["closed"])
            self.assertEqual(self.app.status()["visible"], [])
        self.assertEqual(self.hc.calls, [])

    def test_isolated_close_refuses_a_mismatched_returned_terminal_address(self):
        self.journal.pop("runtime")
        save(self.root / "bridge" / "journal.json", self.journal)
        app = AppSession(self.root)
        app.data = {"terminal": "app-terminal", "terminal_name": "herdr-bridge-app"}
        calls = []

        def hc(args, env, timeout):
            calls.append(args)
            return {"address": "unrelated", "name": "herdr-bridge-app", "argv": ["herdr-app"]}

        app.bridge.runner = hc
        with self.assertRaises(BridgeError):
            app.close()
        self.assertEqual([call[0] for call in calls], ["get"])

    def test_isolated_close_supports_saved_and_historical_names_without_relaunch(self):
        from herdr_agent_bridge.app_session import APP_TERMINAL_NAME, LEGACY_APP_TERMINAL_NAME

        self.journal.pop("runtime")
        save(self.root / "bridge" / "journal.json", self.journal)
        for historical in (False, True):
            with self.subTest(historical=historical):
                app = AppSession(self.root)
                app.data = {"terminal": "app-terminal"}
                expected = LEGACY_APP_TERMINAL_NAME if historical else APP_TERMINAL_NAME
                if not historical:
                    app.data["terminal_name"] = expected
                calls = []

                def hc(args, env, timeout):
                    calls.append(args)
                    return {"address": "app-terminal", "name": expected, "argv": ["herdr-app"]}

                app.bridge.runner = hc
                self.assertTrue(app.close()["closed"])
                self.assertEqual([call[0] for call in calls], ["get", "kill", "rm"])

    def test_new_isolated_app_records_terminal_name_before_launch(self):
        self.journal.pop("runtime")
        save(self.root / "bridge" / "journal.json", self.journal)
        app = AppSession(self.root)

        def hc(args, env, timeout):
            saved = json.loads(app.path.read_text())
            self.assertEqual(saved["terminal_name"], "herdr-bridge-app")
            self.assertEqual(saved["title"], "Herdr Agent Bridge")
            self.assertEqual(args[args.index("--name") + 1], saved["terminal_name"])
            return {"address": "app-terminal"}

        app.bridge.runner = hc
        app.start()

    def test_isolated_app_still_launches_and_uses_private_binding_path(self):
        self.journal.pop("runtime")  # Historical journals default to isolated.
        save(self.root / "bridge" / "journal.json", self.journal)
        self.app = AppSession(self.root)
        self.hc = FakeHC(self.app)
        self.app.bridge.runner = self.hc
        self.assertEqual(self.app.env()["HERDR_APP_HOME"], str(self.root / "scratch" / "app"))
        result = self.app.start(display=":99", title="Isolated")
        self.assertEqual(result["terminal"], "app-terminal")
        calls = []

        def isolated_command(*args):
            calls.append(args)
            if args[:2] == ("do", "space.new"):
                self.assertEqual(json.loads(args[2])["name"], "Isolated")
                return {"space": "new-space", "tab": "setup-tab", "terminal": "setup-terminal"}
            if args == ("ls",):
                return mapping("requested")
            return {}

        with patch.object(self.app, "command", side_effect=isolated_command):
            self.assertEqual(self.app.bind()["binding"], "bound")
        self.assertTrue(any(call[:2] == ("do", "terminal.keep") for call in calls))
        self.assertTrue(any(call[:2] == ("do", "tab.close") for call in calls))
        self.assertIn(["wait", "--timeout", "10", "setup-terminal"], self.hc.calls)
        self.assertIn(["rm", "setup-terminal"], self.hc.calls)


if __name__ == "__main__":
    unittest.main()
