"""Common bridge contracts using disposable files and fake subprocess results."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from herdr_agent_bridge import bridge_common


class BridgeCommonTests(unittest.TestCase):
    def test_directory_fsync_closes_descriptor_even_when_sync_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge_common.fsync_directory(Path(temporary))
            with patch(
                "herdr_agent_bridge.bridge_common.os.fsync", side_effect=OSError("fixture failure")
            ):
                with patch("herdr_agent_bridge.bridge_common.os.close", wraps=os.close) as close:
                    with self.assertRaisesRegex(OSError, "fixture failure"):
                        bridge_common.fsync_directory(Path(temporary))
                    close.assert_called_once()
                    with self.assertRaises(OSError):
                        os.fstat(close.call_args.args[0])

    def test_run_hc_decodes_success_and_preserves_failed_call(self):
        env = {"HC_SESSION": "fixture"}
        with patch(
            "herdr_agent_bridge.bridge_common.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout='{"address":"fixture"}', stderr=""),
        ) as run:
            self.assertEqual(
                bridge_common.run_hc(["get", "fixture"], env, 3), {"address": "fixture"}
            )
            run.assert_called_once_with(
                ["hc", "get", "fixture"], env=env, capture_output=True, text=True, timeout=3
            )
        with patch(
            "herdr_agent_bridge.bridge_common.subprocess.run",
            return_value=SimpleNamespace(
                returncode=9, stdout="", stderr='{"error":{"kind":"blocked"}}'
            ),
        ):
            with self.assertRaises(bridge_common.HcError) as caught:
                bridge_common.run_hc(["send", "fixture"], env, 4)
        self.assertEqual(
            caught.exception.call,
            {
                "args": ["send", "fixture"],
                "exit_code": 9,
                "stdout": "",
                "stderr": '{"error":{"kind":"blocked"}}',
            },
        )

    def test_errors_keep_messages_and_expose_optional_code_and_hc_kind(self):
        error = bridge_common.BridgeError("original message")
        self.assertEqual((str(error), error.code), ("original message", "bridge_operation_failed"))
        error = bridge_common.BridgeError("same message", code="fixture")
        self.assertEqual((str(error), error.code), ("same message", "fixture"))
        call = {"stderr": '{"error":{"kind":"blocked"}}'}
        error = bridge_common.HcError(call)
        self.assertIsInstance(error, bridge_common.BridgeError)
        self.assertEqual(str(error), json.dumps(call, ensure_ascii=False))
        self.assertIs(error.call, call)
        self.assertEqual(bridge_common.hc_error_kind(error), "blocked")
        for stderr in ("not json", "[]", None, "{}", '{"error": []}', '{"error": "private"}'):
            with self.subTest(stderr=stderr):
                self.assertIsNone(
                    bridge_common.hc_error_kind(bridge_common.HcError({"stderr": stderr}))
                )

    def test_save_replaces_private_json_and_digest_hashes_utf8(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "journal.json"
            bridge_common.save(path, {"text": "你好"})
            bridge_common.save(path, {"text": "é"})
            self.assertEqual(path.read_text(), '{\n  "text": "é"\n}\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertEqual(
            bridge_common.digest("é"),
            "4a99557e4033c3539de2eb65472017cad5f9557f7a0625a09f1c3f6e2ba69c4c",
        )


if __name__ == "__main__":
    unittest.main()
