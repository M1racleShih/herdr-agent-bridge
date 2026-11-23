"""Native runtime contracts; fake hc and disposable configuration only."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_pi_bridge import OWNER, FakeHC

from herdr_agent_bridge.agent_bridge import Bridge, BridgeError, save
from herdr_agent_bridge.project_bridge import Project


class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.hc = FakeHC()

    def bridge(self, **kwargs):
        bridge = Bridge.create(
            self.base / "run", self.project, agent="pi", runtime="native", runner=self.hc, **kwargs
        )
        self.hc.terminal["labels"] = {"bridge_run": bridge.data["session"]}
        return bridge

    def test_native_preserves_configuration_and_uses_normal_cli(self):
        home = self.base / "normal-pi"
        home.mkdir()
        settings = home / "settings.json"
        settings.write_text('{"extensions":["my-extension"],"skills":["my-skill"]}')
        before = settings.read_bytes()
        with patch.dict(
            os.environ,
            {
                "PI_CODING_AGENT_DIR": str(home),
                "HC_SESSION": "normal-core",
                "PATH": "/fixture/bin:/usr/bin",
                "XDG_CONFIG_HOME": str(self.base / "xdg"),
                "PI_SESSION_FILE": "/parent/session.jsonl",
            },
        ):
            bridge = self.bridge(model="openai/gpt-6.1-sol", thinking="high")
        with patch.dict(os.environ, {"HC_SESSION": "other-core", "PI_CODING_AGENT_DIR": "/other"}):
            bridge.launch()
        args, env, _ = self.hc.calls[-1]
        self.assertEqual(env["HC_SESSION"], "normal-core")
        self.assertEqual(env["PI_CODING_AGENT_DIR"], str(home))
        self.assertNotIn("PI_SESSION_FILE", env)
        self.assertIn("PI_CODING_AGENT_DIR=" + str(home), args)
        self.assertIn("HC_SESSION=normal-core", args)
        self.assertIn("PATH=/fixture/bin:/usr/bin", args)
        self.assertIn("pi", args)
        self.assertIn("-e", args)
        self.assertNotIn("--tools", args)
        self.assertFalse(any(arg.startswith("--no-") for arg in args[args.index("pi") + 1 :]))
        self.assertNotIn("pi_oauth_session.mjs", " ".join(args))
        self.assertEqual(settings.read_bytes(), before)
        self.assertFalse((Path(bridge.data["scratch"]) / "pi" / "settings.json").exists())
        self.assertNotEqual(bridge.data["terminal"], "agent")

    def test_native_cleanup_checks_label_and_never_stops_shared_core(self):
        bridge = self.bridge()
        bridge.launch()
        self.hc.terminal["labels"]["bridge_run"] = "someone-else"
        with self.assertRaises(BridgeError):
            bridge.close()
        self.assertEqual(self.hc.count("kill"), 0)
        self.hc.terminal["labels"]["bridge_run"] = bridge.data["session"]
        bridge.close()
        self.assertTrue(bridge.data["closed"])
        self.assertEqual(self.hc.count("kill"), 1)
        self.assertEqual(self.hc.count("server"), 0)
        self.assertEqual(self.hc.count("ls"), 0)
        self.assertTrue(Path(bridge.data["scratch"]).exists())

    def test_settled_matches_exact_session_and_final_message(self):
        bridge = self.bridge()
        evidence = {"thread_id": "s1", "final_message_id": "m2"}
        self.assertFalse(bridge.settled(evidence))
        path = bridge.root / "pi-settled.json"
        record = {
            "state": "settled",
            "session_id": "s1",
            "final_message_id": "m2",
            "aborted": False,
        }
        save(path, record)
        self.assertTrue(bridge.settled(evidence))
        for field, value in [
            ("state", "working"),
            ("session_id", "other"),
            ("final_message_id", "old"),
            ("aborted", True),
        ]:
            save(path, {**record, field: value})
            self.assertFalse(bridge.settled(evidence))

    def test_native_observe_checks_owner_before_wait_or_read(self):
        bridge = self.bridge()
        bridge.launch()
        self.hc.terminal["labels"]["bridge_run"] = "someone-else"
        with self.assertRaises(BridgeError):
            bridge.observe(wait=1)
        self.assertEqual(self.hc.count("wait"), 0)
        self.assertEqual(self.hc.count("read"), 0)

    def test_new_projects_default_native_existing_requests_keep_runtime(self):
        config = self.base / "project.json"
        entry = Project.initialize(config, "fixture", self.project, OWNER)
        self.assertEqual(entry.configuration()["runtime"], "native")
        entry = Project(config, runner=self.hc)
        result = entry.start("original")
        self.assertEqual(result["runtime"], "native")
        entry.runtime("isolated")
        repeated = entry.start("original")
        self.assertEqual(repeated["runtime"], "native")
        self.assertEqual(self.hc.count("run"), 1)

    def test_old_config_and_record_remain_isolated(self):
        config = self.base / "project.json"
        Project.initialize(config, "fixture", self.project, OWNER)
        data = json.loads(config.read_text())
        data.pop("runtime")
        save(config, data)
        self.assertEqual(Project(config).configuration()["runtime"], "isolated")

    def test_native_refuses_home_copy_and_unsupported_agent_before_creation(self):
        with self.assertRaises(BridgeError):
            self.bridge(source_home=self.base)
        self.assertFalse((self.base / "run").exists())
        with self.assertRaisesRegex(BridgeError, "unsupported coding agent"):
            Bridge.create(self.base / "run", self.project, agent="unknown", runtime="native")
        self.assertFalse((self.base / "run").exists())


if __name__ == "__main__":
    unittest.main()
