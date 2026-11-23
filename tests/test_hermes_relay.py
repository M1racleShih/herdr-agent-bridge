import json
import sys
import tempfile
import unittest
from pathlib import Path

from herdr_agent_bridge.bridge_common import BridgeError, save
from herdr_agent_bridge.grok_notifications import Outbox
from herdr_agent_bridge.project_bridge import Project

OWNER = {"bot_id": "test-bot", "conversation_id": "original-session"}
CONTEXT = {"project_id": "project-test", "request_id": "original-request"}
PAYLOAD = {
    "event_id": "evt-test",
    "run_id": "run-coding-agent",
    "owner": OWNER,
    "task_id": "round-one",
    "kind": "result_available",
    "result": {"answer": "Do not transmit task content"},
}

FAKE_HERMES = """
import json
from pathlib import Path


def read_delivery_result(home, key):
    path = Path(home) / (key + '.json')
    return json.loads(path.read_text()) if path.exists() else None


def find_canonical_live_owner(home):
    home = Path(home)
    mode = (home / 'mode').read_text()
    if mode == 'down':
        raise ValueError('Gateway unavailable')
    return {'profile_home': str(home), 'canonical': True,
            'session_id': 'replacement-session' if mode == 'replaced' else 'original-session'}


def deliver_to_live_owner(home, owner, message, *, delivery_id):
    home = Path(home)
    mode = (home / 'mode').read_text()
    with (home / 'submissions').open('a') as stream:
        stream.write(delivery_id + '\\n')
    receipt = {'status': 'queued', 'session_id': owner['session_id'], 'profile_home': str(home),
               'delivery_id': delivery_id, 'message': message, 'admission_id': 'durable-admission'}
    (home / (delivery_id + '.json')).write_text(json.dumps(receipt))
    if mode == 'lost':
        raise RuntimeError('HTTP reply lost after durable admission')
    return receipt
"""

FAKE_PERSISTENCE = """
from contextlib import contextmanager
from types import SimpleNamespace


@contextmanager
def open_persistence_reader(home):
    yield SimpleNamespace(sessions=SimpleNamespace(get=lambda sid:
        SimpleNamespace(session_id=sid, source='local', chat_id='local-fixture')))
"""


class HermesRelayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()
        self.home = self.base / "profiles" / OWNER["bot_id"]
        self.home.mkdir(parents=True)
        self.mode("ready")
        source = self.base / "hermes-agent"
        (source / "tools").mkdir(parents=True)
        (source / "tools" / "__init__.py").touch()
        (source / "tools" / "bot_live_delivery.py").write_text(FAKE_HERMES)
        (source / "hermes_bootstrap.py").touch()
        (source / "persistence_assembly.py").write_text(FAKE_PERSISTENCE)
        self.config = self.base / "relay.json"
        save(
            self.config,
            {
                **OWNER,
                "transport": "hermes_relay",
                "profile_home": str(self.home),
                "hermes_source": str(source),
                "hermes_python": str(Path(sys.executable).resolve()),
            },
        )
        self.outbox = Outbox(self.base / "run", OWNER)
        self.outbox.enqueue(PAYLOAD)

    def mode(self, name):
        (self.home / "mode").write_text(name)

    def test_one_delivery_to_original_chat_uses_persisted_pointer_and_readback(self):
        event = self.outbox.deliver(PAYLOAD["event_id"], self.config, relay_context=CONTEXT)
        self.assertEqual(event["state"], "accepted")
        self.assertEqual(event["relay"]["status"], "queued")
        self.assertIsNone(event["acknowledgment"])
        key = event["relay"]["delivery_id"]
        self.assertEqual((self.home / "submissions").read_text().splitlines(), [key])
        record = self.home / (key + ".json")
        completion = json.loads(record.read_text())
        self.assertEqual(completion["session_id"], OWNER["conversation_id"])
        for value in (
            *CONTEXT.values(),
            PAYLOAD["event_id"],
            PAYLOAD["run_id"],
            PAYLOAD["task_id"],
        ):
            self.assertIn(value, completion["message"])
        self.assertNotIn(PAYLOAD["result"]["answer"], completion["message"])
        completion.update(status="settled", reply="Do not persist the bot reply")
        save(record, completion)
        checked = self.outbox.reconcile_relay(PAYLOAD["event_id"], self.config, CONTEXT)
        self.assertEqual(checked["relay"], {"delivery_id": key, "status": "settled"})
        self.assertEqual(len(checked["attempts"]), 1)
        self.assertEqual((self.home / "submissions").read_text().splitlines(), [key])
        self.assertNotIn(
            completion["reply"], (self.base / "run" / "notifications.json").read_text()
        )

    def test_hermes_bootstrap_can_reexec_without_consuming_the_event(self):
        source = self.base / "hermes-agent" / "hermes_bootstrap.py"
        source.write_text("""import os, sys
if os.environ.get("RELAY_FIXTURE_BOOTSTRAP") != "reexecuted":
    os.environ["RELAY_FIXTURE_BOOTSTRAP"] = "reexecuted"
    os.execv(sys.executable, [sys.executable, "-I", *sys.argv])
""")
        got = self.outbox.deliver(PAYLOAD["event_id"], self.config, relay_context=CONTEXT)
        self.assertEqual((got["state"], got["relay"]["status"]), ("accepted", "queued"))
        self.assertEqual(len((self.home / "submissions").read_text().splitlines()), 1)

    def test_lost_reply_is_reconciled_without_sending_again(self):
        self.mode("lost")
        event_id = PAYLOAD["event_id"]
        lost = self.outbox.deliver(event_id, self.config, relay_context=CONTEXT)
        self.assertEqual(lost["state"], "unknown")
        with self.assertRaises(BridgeError):
            self.outbox.deliver(event_id, self.config, relay_context=CONTEXT, retry=True)
        checked = self.outbox.reconcile_relay(event_id, self.config, CONTEXT)
        self.assertEqual((checked["state"], checked["relay"]["status"]), ("accepted", "queued"))
        self.assertEqual(
            (self.home / "submissions").read_text().splitlines(), [lost["relay"]["delivery_id"]]
        )

    def test_replacement_chat_is_not_used_and_retry_stays_bound_to_original(self):
        self.mode("replaced")
        event_id = PAYLOAD["event_id"]
        event = self.outbox.deliver(event_id, self.config, relay_context=CONTEXT)
        self.assertEqual((event["state"], event["relay"]["status"]), ("rejected", "not_sent"))
        self.assertFalse((self.home / "submissions").exists())
        self.mode("ready")
        recovered = self.outbox.deliver(event_id, self.config, relay_context=CONTEXT, retry=True)
        self.assertEqual((recovered["state"], recovered["relay"]["status"]), ("accepted", "queued"))
        self.assertEqual(recovered["relay"]["delivery_id"], event["relay"]["delivery_id"])

    def test_project_watch_sends_saved_event_without_a_manual_notify(self):
        from test_pi_bridge import FakeHC

        project_root = self.base / "work"
        project_root.mkdir()
        pi_home = self.base / "pi"
        pi_home.mkdir()
        (pi_home / "settings.json").write_text("{}")
        (pi_home / "auth.json").write_text("{}")
        project_config = self.base / "project.json"
        Project.initialize(
            project_config,
            CONTEXT["project_id"],
            project_root,
            OWNER,
            webhook_config=self.config,
            runtime="isolated",
        )
        hc = FakeHC()
        project = Project(project_config, runner=hc)
        request = CONTEXT["request_id"]
        started = project.start(request, source_home=pi_home)
        self.addCleanup(project.close, request)
        (project_root / started["run"] / "bridge" / "pi-selection.json").write_text(
            json.dumps(
                {
                    **started["execution_policy"]["resolved"],
                    "state": "ready",
                    "session_id": "fixture-session",
                }
            )
        )
        manager = project.manager(project.record(request)[1])
        report = manager.watch(seconds=2)
        self.assertEqual(report["watch_status"], "outcome")
        event = report["notification"]
        self.assertEqual((event["state"], event["payload"]["kind"]), ("accepted", "run_ready"))
        self.assertEqual(event["relay"]["status"], "queued")
        observed = project.notify(report["event_id"], request)["notification"]
        self.assertEqual(observed["relay"], event["relay"])
        self.assertEqual(len((self.home / "submissions").read_text().splitlines()), 1)
        with self.assertRaisesRegex(BridgeError, "read the original Hermes Relay receipt"):
            project.acknowledge(report["event_id"], "not-an-observed-receipt", request)

        original = json.loads(self.config.read_text())
        other = self.base / "other" / "profiles" / OWNER["bot_id"]
        try:
            save(self.config, {**original, "profile_home": str(other)})
            with self.assertRaisesRegex(BridgeError, "Hermes Relay route changed"):
                project.notify(report["event_id"], request)
        finally:
            save(self.config, original)
