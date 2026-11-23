"""Event cursor contracts, independent of hc and user sessions."""

import json
import os
import subprocess
import sys
import time
import unittest

from herdr_agent_bridge.bridge_common import BridgeError
from herdr_agent_bridge.core_events import CoreEvents, EventCursor


class EventCursorTests(unittest.TestCase):
    def test_snapshot_events_and_replayed_sequence_only_wake_once(self):
        cursor = EventCursor("t1")
        self.assertTrue(cursor.accept({"seq": 10, "terminals": [{"address": "t1"}]}))
        event = {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "seq": 11,
                "terminal": "t1",
                "type": "status",
                "data": {"status": {"state": "done"}},
            },
        }
        self.assertTrue(cursor.accept(event))
        self.assertFalse(cursor.accept(event))
        self.assertFalse(cursor.accept({**event, "params": {**event["params"], "seq": 9}}))
        self.assertEqual(cursor.stats["duplicates"], 2)
        self.assertEqual(cursor.stats["last_seq"], 11)

    def test_missed_snapshot_reconciles_and_reconnect_accepts_a_new_sequence_epoch(self):
        cursor = EventCursor("t1")
        cursor.accept({"seq": 100, "terminals": [{"address": "t1"}]})
        self.assertTrue(
            cursor.accept(
                {
                    "jsonrpc": "2.0",
                    "method": "missed",
                    "params": {"seq": 120, "terminals": [{"address": "t1"}]},
                }
            )
        )
        self.assertEqual(cursor.stats["last_seq"], 120)
        cursor.new_connection()
        self.assertTrue(cursor.accept({"seq": 2, "terminals": [{"address": "t1"}]}))
        self.assertEqual(cursor.stats["last_seq"], 2)
        self.assertEqual(cursor.stats["snapshots"], 3)

    def test_invalid_or_other_terminal_records_are_rejected_without_echoing_private_input(self):
        invalid = [
            None,
            {"seq": True, "terminals": []},
            {"seq": 1, "terminals": [{"address": "private-terminal"}]},
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "seq": 2,
                    "terminal": "private-terminal",
                    "data": {"secret": "private-value"},
                },
            },
        ]
        for message in invalid:
            cursor = EventCursor("t1")
            with self.subTest(message=message), self.assertRaises(BridgeError) as raised:
                cursor.accept(message)
            self.assertNotIn("private", str(raised.exception))


class CoreEventsTests(unittest.TestCase):
    def test_real_pipe_eof_reconnects_and_reconciles_a_fresh_snapshot(self):
        children = []

        def open_pipe(argv, **kwargs):
            seq = 100 if not children else 1
            snapshot = {"seq": seq, "terminals": [{"address": "t1"}]}
            event = {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"seq": seq + 1, "terminal": "t1", "type": "status", "data": {}},
            }
            output = "\n".join(json.dumps(item) for item in [snapshot, event, event]) + "\n"
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", "print(" + repr(output) + ',end="")'], **kwargs
            )
            children.append(process)
            return process

        with CoreEvents("t1", os.environ, opener=open_pipe) as source:
            deadline = time.monotonic() + 5
            while (
                source.stats["connections"] < 2 or source.stats["snapshots"] < 2
            ) and time.monotonic() < deadline:
                source.read(1)
            self.assertGreaterEqual(source.stats["snapshots"], 2)
            self.assertGreaterEqual(source.stats["duplicates"], 1)
            self.assertGreaterEqual(source.stats["reconnects"], 1)
            self.assertLess(source.stats["last_seq"], 100)
        self.assertTrue(all(process.poll() is not None for process in children))

    def test_oversized_partial_record_stops_and_reaps_the_owned_subscriber(self):
        children = []

        def open_pipe(argv, **kwargs):
            process = subprocess.Popen(
                [sys.executable, "-c", 'import sys; sys.stdout.write("x" * 1100000)'], **kwargs
            )
            children.append(process)
            return process

        with (
            self.assertRaises(BridgeError),
            CoreEvents("t1", os.environ, opener=open_pipe) as source,
        ):
            source.read(3)
        self.assertIsNotNone(children[0].poll())
