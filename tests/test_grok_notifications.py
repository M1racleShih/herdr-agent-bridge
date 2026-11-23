"""Outbox contract checks; all webhooks are fake or local disposable fixtures."""

import copy
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from herdr_agent_bridge import grok_notifications as notify

OWNER = {"bot_id": "bot-fixture", "conversation_id": "conversation-fixture"}
OTHER = {"bot_id": "another-bot", "conversation_id": "conversation-fixture"}
KEY = "test-webhook-secret"
URL = "https://example.invalid/fixture/webhook"


def payload(event_id="event-1", **kwargs):
    return {
        "event_id": event_id,
        "run_id": "run-1",
        "owner": copy.deepcopy(OWNER),
        "task_id": "task-1",
        "kind": "result",
        "state": "evidence_ready",
        "verified_success": False,
        **kwargs,
    }


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-notify-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.write_config()
        self.box = notify.Outbox(self.root / "run", OWNER)

    def write_config(self, **overrides):
        values = {"url": URL, "key": KEY, **OWNER, **overrides}
        self.config.write_text(json.dumps(values))
        self.config.chmod(0o600)

    def assert_no_config_secrets(self, value):
        for content in (json.dumps(value), self.box.path.read_text()):
            self.assertNotIn(KEY, content)
            self.assertNotIn(URL, content)

    def test_enqueue_deduplicates_identical_payload_and_binds_owner_and_id(self):
        original = payload(result={"answer": "fixture"})
        first = self.box.enqueue(original)
        original["result"]["answer"] = "changed in caller"
        self.assertEqual(self.box.get("event-1")["payload"]["result"], {"answer": "fixture"})
        same = payload(result={"answer": "fixture"})
        self.assertEqual(self.box.enqueue(same), first)
        changed = payload(result={"answer": "different"})
        with self.assertRaises(notify.BridgeError):
            self.box.enqueue(changed)
        wrong_owner = payload()
        wrong_owner["owner"] = OTHER
        with self.assertRaises(notify.BridgeError):
            self.box.enqueue(wrong_owner)
        with self.assertRaises(notify.BridgeError):
            notify.Outbox(self.box.root, OTHER)
        self.assertEqual(self.box.get("event-1"), first)
        self.assertEqual(len(json.loads(self.box.path.read_text())["events"]), 1)

    def test_boolean_and_number_payload_changes_are_not_identical(self):
        self.box.enqueue(payload(result={"count": 1}))
        with self.assertRaises(notify.BridgeError):
            self.box.enqueue(payload(result={"count": True}))
        with self.assertRaises(notify.BridgeError):
            self.box.enqueue(payload(result={"count": 1.0}))

    def test_auth_headers_and_known_credentials_are_not_owner_identifiers(self):
        for field in ("bot_id", "conversation_id"):
            for value in (
                "Authorization: Bearer " + KEY,
                "  bEaReR " + KEY,
                "X-Api-Key: " + KEY,
                KEY,
                "prefix-" + KEY,
            ):
                owner = {**OWNER, field: value}
                self.write_config(**owner)
                with self.subTest(field=field, header=value.startswith("Authorization")):
                    with self.assertRaises(notify.BridgeError) as error:
                        notify.load_config(self.config, owner)
                    self.assertNotIn(KEY, str(error.exception))
        self.write_config()
        for value in ("Authorization: Bearer " + KEY, "bearer " + KEY, "Bearer\u00a0" + KEY):
            with self.assertRaises(notify.BridgeError):
                notify.Outbox(self.root / "must-not-exist", {**OWNER, "bot_id": value})
            self.assertFalse((self.root / "must-not-exist").exists())

    def test_known_secret_cannot_be_enqueued_or_delivered(self):
        guarded = notify.Outbox(self.root / "guarded", OWNER, credential=KEY)
        with self.assertRaises(notify.BridgeError) as error:
            guarded.enqueue(payload(state={"nested": ["prefix-" + KEY]}))
        self.assertNotIn(KEY, str(error.exception))
        self.assertEqual(json.loads(guarded.path.read_text())["events"], {})
        self.box.enqueue(payload(state={"nested": ["prefix-" + KEY]}))
        before = self.box.get("event-1")
        calls = []
        with self.assertRaises(notify.BridgeError) as error:
            self.box.deliver("event-1", self.config, sender=lambda *args: calls.append(args) or 200)
        self.assertNotIn(KEY, str(error.exception))
        self.assertEqual(self.box.get("event-1"), before)
        self.assertEqual(before["state"], "pending")
        self.assertEqual(before["attempts"], [])
        self.assertEqual(calls, [])
        with self.assertRaises(notify.BridgeError):
            notify.Outbox(self.box.root, OWNER, credential=KEY).get("event-1")

    def test_escaped_secrets_in_values_or_object_keys_fail_before_post(self):
        key = 'fixture-quote"-and-backslash\\-credential'
        self.write_config(key=key)
        for index, value in enumerate(({"nested": ["prefix-" + key]}, {key: "value"})):
            event_id = "escaped-" + str(index)
            self.box.enqueue(payload(event_id, result=value))
            calls = []
            with self.assertRaises(notify.BridgeError) as error:
                self.box.deliver(
                    event_id, self.config, sender=lambda *args: calls.append(args) or 200
                )
            self.assertNotIn(key, str(error.exception))
            event = self.box.get(event_id)
            self.assertEqual(event["state"], "pending")
            self.assertEqual(event["attempts"], [])
            self.assertEqual(calls, [])

    def test_legacy_event_owner_is_revalidated_on_every_return_path(self):
        self.box.enqueue(payload())
        original = json.loads(self.box.path.read_text())
        for owner in (
            {**OWNER, "bot_id": "Authorization: Bearer " + KEY},
            {**OWNER, "conversation_id": KEY},
            OTHER,
        ):
            data = copy.deepcopy(original)
            data["events"]["event-1"]["payload"]["owner"] = owner
            data["events"]["event-1"]["state"] = "accepted"
            notify.save(self.box.path, data)
            calls = []
            for action in (
                lambda: self.box.get("event-1"),
                lambda: self.box.acknowledge("event-1", OWNER, "receipt"),
                lambda: self.box.deliver(
                    "event-1", self.config, sender=lambda *args: calls.append(args) or 200
                ),
            ):
                with self.assertRaises(notify.BridgeError) as error:
                    action()
                self.assertNotIn(KEY, str(error.exception))
            self.assertEqual(json.loads(self.box.path.read_text()), data)
            self.assertEqual(calls, [])

    def test_run_pointer_is_restricted_to_dedicated_run(self):
        event = payload(run=".artifacts/runs/dedicated-run")
        result = self.box.enqueue(event)
        self.assertEqual(result["payload"]["run"], ".artifacts/runs/dedicated-run")
        for pointer in (
            "/absolute",
            ".artifacts/runs",
            ".artifacts/runs/../other",
            ".artifacts/runs/./other",
            ".artifacts/runs//other",
            ".artifacts\\runs\\other",
            "other/runs/run",
            ".artifacts/runs/run\nsecret",
        ):
            with self.subTest(pointer=pointer):
                with self.assertRaises(notify.BridgeError):
                    self.box.enqueue(payload("bad-path", run=pointer))
        self.assertEqual(len(json.loads(self.box.path.read_text())["events"]), 1)

    def test_historical_outbox_pointer_is_preserved_and_unknown_delivery_never_replays(self):
        from herdr_agent_bridge.runtime_paths import LEGACY_RUNS

        pointer = str(LEGACY_RUNS / "saved-event")
        self.box.enqueue(payload(run=pointer))
        self.box.deliver("event-1", self.config, sender=lambda *args: None)
        before = self.box.path.read_bytes()
        reopened = notify.Outbox(self.box.path.parent, OWNER)
        self.assertEqual(reopened.get("event-1")["payload"]["run"], pointer)
        calls = []
        for retry in (False, True):
            with self.assertRaises(notify.BridgeError):
                reopened.deliver(
                    "event-1", self.config, sender=lambda *args: calls.append(args), retry=retry
                )
        self.assertEqual(calls, [])
        self.assertEqual(self.box.path.read_bytes(), before)

    def test_null_task_association_is_limited_to_run_level_events(self):
        for kind in ("permission_required", "run_ready", "agent_error", "exited"):
            with self.subTest(kind=kind):
                event = payload("event-" + kind, kind=kind, task_id=None)
                result = self.box.enqueue(event)
                self.assertEqual(result["payload"]["kind"], kind)
                self.assertIsNone(result["payload"]["task_id"])
        for kind in ("result", "result_available", "question", "unexpected_kind"):
            with self.subTest(kind=kind):
                with self.assertRaises(notify.BridgeError):
                    self.box.enqueue(payload("bad-" + kind, kind=kind, task_id=None))
        for task_id in ("", 1):
            with self.subTest(task_id=task_id):
                with self.assertRaises(notify.BridgeError):
                    self.box.enqueue(payload("bad-task", task_id=task_id))
        self.assertEqual(len(json.loads(self.box.path.read_text())["events"]), 4)

    def test_intent_precedes_post_and_200_is_only_trigger_acceptance(self):
        self.box.enqueue(payload(result={"answer": "fixture"}, evidence={"check": "fixture"}))
        calls = []

        def sender(url, key, body):
            saved = json.loads(self.box.path.read_text())["events"]["event-1"]
            self.assertEqual(saved["state"], "unknown")
            self.assertEqual(len(saved["attempts"]), 1)
            self.assertEqual(json.loads(body), saved["payload"])
            calls.append((url, key))
            return 200

        result = self.box.deliver("event-1", self.config, sender=sender)
        self.assertEqual(calls, [(URL, KEY)])
        self.assertEqual(result["state"], "accepted")
        self.assertEqual(result["attempts"][0]["status_code"], 200)
        self.assertIsNone(result["acknowledgment"])
        self.assertFalse(result["payload"]["verified_success"])
        self.assertNotIn("verified_success", result)
        self.assertNotIn("observed_receipt", result)
        self.assert_no_config_secrets(result)
        with self.assertRaises(notify.BridgeError):
            self.box.deliver("event-1", self.config, sender=sender, retry=True)
        self.assertEqual(len(calls), 1)

    def test_uncertain_failure_and_lost_reply_never_replay(self):
        for name, failure in (
            ("timeout", TimeoutError(KEY + URL)),
            ("lost", OSError(KEY + URL)),
            ("invalid-status", None),
        ):
            with self.subTest(name=name):
                event_id = "event-" + name
                self.box.enqueue(payload(event_id))
                calls = []

                def sender(url, key, body):
                    calls.append(1)
                    if failure is not None:
                        raise failure
                    return None

                result = self.box.deliver(event_id, self.config, sender=sender)
                self.assertEqual(result["state"], "unknown")
                expected = {
                    "timeout": "timeout",
                    "lost": "transport_error",
                    "invalid-status": "invalid_status",
                }[name]
                self.assertEqual(result["attempts"][0]["error_type"], expected)
                reopened = notify.Outbox(self.box.path.parent, OWNER)
                for retry in (False, True):
                    with self.assertRaises(notify.BridgeError):
                        reopened.deliver(event_id, self.config, sender=sender, retry=retry)
                self.assertEqual(calls, [1])
                self.assert_no_config_secrets(result)

    def test_process_interruption_leaves_unknown_before_post_returns(self):
        self.box.enqueue(payload())

        class Interrupted(BaseException):
            pass

        def interrupted(url, key, body):
            raise Interrupted()

        with self.assertRaises(Interrupted):
            self.box.deliver("event-1", self.config, sender=interrupted)
        reopened = notify.Outbox(self.box.path.parent, OWNER)
        self.assertEqual(reopened.get("event-1")["state"], "unknown")
        with self.assertRaises(notify.BridgeError):
            reopened.deliver("event-1", self.config, sender=lambda *args: 200, retry=True)

    def test_ambiguous_http_reply_preserves_unknown_delivery_and_cannot_be_retried(self):
        for status in (202, 408, 500, 502, 503, 504):
            with self.subTest(status=status):
                event_id = "ambiguous-" + str(status)
                self.box.enqueue(payload(event_id))
                calls = []

                def sender(*args):
                    calls.append(args)
                    return status

                event = self.box.deliver(event_id, self.config, sender=sender)
                self.assertEqual(event["state"], "unknown")
                self.assertEqual(event["attempts"][0]["status_code"], status)
                for retry in (False, True):
                    with self.assertRaises(notify.BridgeError):
                        self.box.deliver(event_id, self.config, sender=sender, retry=retry)
                self.assertEqual(len(calls), 1)

    def test_retry_requires_confirmed_rejection_and_explicit_flag(self):
        self.box.enqueue(payload())
        responses = iter((429, 200))
        calls = []

        def sender(url, key, body):
            calls.append(1)
            return next(responses)

        with self.assertRaises(notify.BridgeError):
            self.box.deliver("event-1", self.config, sender=sender, retry=True)
        self.assertEqual(calls, [])
        first = self.box.deliver("event-1", self.config, sender=sender)
        self.assertEqual(first["state"], "rejected")
        self.assertEqual(first["attempts"][0]["status_code"], 429)
        with self.assertRaises(notify.BridgeError):
            self.box.deliver("event-1", self.config, sender=sender)
        self.write_config(conversation_id="not-the-owner")
        with self.assertRaises(notify.BridgeError):
            self.box.deliver("event-1", self.config, sender=sender, retry=True)
        self.assertEqual(self.box.get("event-1"), first)
        self.write_config()
        second = self.box.deliver("event-1", self.config, sender=sender, retry=True)
        self.assertEqual(second["state"], "accepted")
        self.assertEqual([a["status_code"] for a in second["attempts"]], [429, 200])
        self.assertEqual(calls, [1, 1])

    def test_config_permissions_symlinks_owner_and_url_fail_before_mutation(self):
        self.box.enqueue(payload())
        before = self.box.get("event-1")
        calls = []

        def rejected(path=None):
            with self.assertRaises(notify.BridgeError) as captured:
                self.box.deliver(
                    "event-1", path or self.config, sender=lambda *args: calls.append(args) or 200
                )
            self.assertNotIn(KEY, str(captured.exception))
            self.assertNotIn(URL, str(captured.exception))
            self.assertEqual(self.box.get("event-1"), before)

        self.config.chmod(0o640)
        rejected()
        self.config.chmod(0o604)
        rejected()
        self.config.chmod(0o600)
        link = self.root / "link.json"
        link.symlink_to(self.config)
        rejected(link)
        self.write_config(bot_id="wrong-owner")
        rejected()
        for endpoint in (
            "http://example.invalid/hook",
            "https://user:pass@example.invalid/hook",
            "https://example.invalid/hook#fragment",
            "https://example.invalid/hook?token=secret",
            "https://example.invalid/hook?",
            "https://example.invalid:99999/hook",
            "https://example.invalid/\nhook",
        ):
            with self.subTest(endpoint=endpoint):
                self.write_config(url=endpoint)
                rejected()
        self.config.write_text('{"url":"https://a.invalid", "url":"https://b.invalid"}')
        rejected()
        self.assertEqual(calls, [])

    def test_loopback_http_requires_explicitly_injected_test_sender(self):
        self.box.enqueue(payload())
        self.write_config(url="http://127.0.0.1:8080/fixture")
        with self.assertRaises(notify.BridgeError):
            notify.load_config(self.config, OWNER)
        with self.assertRaises(notify.BridgeError):
            self.box.deliver("event-1", self.config)
        self.assertEqual(self.box.get("event-1")["state"], "pending")
        self.assertEqual(
            self.box.deliver("event-1", self.config, sender=lambda *args: 200)["state"], "accepted"
        )

    def test_redirects_are_rejected_without_forwarding_authorization(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.headers.get("Authorization")))
                code = int(self.path.lstrip("/")) if self.path != "/target" else 200
                self.send_response(code)
                if code != 200:
                    self.send_header("Location", "/target")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close_server():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.addCleanup(close_server)
        for code in (301, 302, 303, 307, 308):
            with self.subTest(status=code):
                self.write_config(url=f"http://127.0.0.1:{server.server_port}/{code}")
                event_id = f"event-{code}"
                self.box.enqueue(payload(event_id))
                result = self.box.deliver(event_id, self.config, sender=notify._post)
                self.assertEqual(result["state"], "rejected")
                self.assertEqual(result["attempts"][0]["status_code"], code)
        self.assertEqual(
            requests, [(f"/{code}", "Bearer " + KEY) for code in (301, 302, 303, 307, 308)]
        )
        self.assertNotIn(KEY, self.box.path.read_text())
        self.assertNotIn("127.0.0.1", self.box.path.read_text())

    def test_acknowledgment_binds_event_and_owner_without_inference(self):
        for event_id in ("accepted", "unknown", "pending"):
            self.box.enqueue(payload(event_id))
        self.box.deliver("accepted", self.config, sender=lambda *args: 200)
        self.box.deliver("unknown", self.config, sender=lambda *args: None)
        before = self.box.get("accepted")
        with self.assertRaises(notify.BridgeError):
            self.box.acknowledge("accepted", OTHER, "receipt-1")
        with self.assertRaises(notify.BridgeError):
            self.box.acknowledge("missing", OWNER, "receipt-1")
        with self.assertRaises(notify.BridgeError):
            self.box.acknowledge("pending", OWNER, "receipt-1")
        self.assertEqual(self.box.get("accepted"), before)
        result = self.box.acknowledge("accepted", OWNER, "receipt-1")
        self.assertEqual(result["state"], "accepted")
        self.assertEqual(result["acknowledgment"]["owner"], OWNER)
        self.assertEqual(result["acknowledgment"]["receipt"], "receipt-1")
        self.assertFalse(result["payload"]["verified_success"])
        self.assertEqual(self.box.acknowledge("accepted", OWNER, "receipt-1"), result)
        with self.assertRaises(notify.BridgeError):
            self.box.acknowledge("accepted", OWNER, "different-receipt")
        with self.assertRaises(notify.BridgeError):
            self.box.acknowledge("unknown", OWNER, "receipt-1")
        unknown = self.box.acknowledge("unknown", OWNER, "receipt-2")
        self.assertEqual(unknown["state"], "unknown")
        self.assertEqual(unknown["acknowledgment"]["receipt"], "receipt-2")
        with self.assertRaises(notify.BridgeError):
            self.box.deliver("unknown", self.config, sender=lambda *args: 200, retry=True)

    def test_concurrent_deliveries_send_only_once(self):
        self.box.enqueue(payload())
        start = threading.Barrier(2)
        calls, results, failures = [], [], []

        def sender(url, key, body):
            calls.append(1)
            return 200

        def attempt():
            start.wait(timeout=5)
            try:
                results.append(self.box.deliver("event-1", self.config, sender=sender))
            except notify.BridgeError as exc:
                failures.append(str(exc))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual([result["state"] for result in results], ["accepted"])
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()
