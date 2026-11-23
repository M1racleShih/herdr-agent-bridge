"""Shared policy and wire contracts; no agents, SDK or network access."""

import json
import shutil
import subprocess
import unittest

from herdr_agent_bridge.bridge_common import BridgeError
from herdr_agent_bridge.bridge_protocol import PI_THINKING_LEVELS, ExecutionPolicy, policy
from herdr_agent_bridge.resource_paths import resource_path


class ExecutionPolicyTests(unittest.TestCase):
    def test_pi_and_codex_enforce_their_own_execution_policy(self):
        codex = {"model": "gpt-6.2-sol", "thinking": "high"}
        self.assertEqual(policy(codex, agent="codex"), codex)
        self.assertEqual(
            policy({"model": "provider/pi-model", "thinking": "off"}),
            {"model": "provider/pi-model", "thinking": "off"},
        )
        for agent, selected in (
            ("pi", codex),
            ("codex", {**codex, "thinking": "off"}),
            ("codex", {**codex, "model": "--other-option"}),
        ):
            with self.subTest(agent=agent, selected=selected), self.assertRaises(BridgeError):
                policy(selected, agent=agent)

    def test_partial_override_keeps_default_model_and_records_each_source(self):
        defaults = {"model": "openai/gpt-6.1-sol", "thinking": "high"}
        selected = ExecutionPolicy.resolve(defaults, thinking="medium")
        defaults["model"] = "changed/default"
        self.assertEqual(
            selected.to_dict(),
            {
                "requested": {"model": None, "thinking": "medium"},
                "resolved": {"model": "openai/gpt-6.1-sol", "thinking": "medium"},
                "sources": {"model": "project_default", "thinking": "task_override"},
            },
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_javascript_and_python_accept_the_same_wire_thinking_levels(self):
        module = resource_path("bridge_protocol.mjs")
        script = f"import {{ thinkingLevels }} from {json.dumps(module.as_uri())}; console.log(JSON.stringify([...thinkingLevels]));"
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=5
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), list(PI_THINKING_LEVELS))
