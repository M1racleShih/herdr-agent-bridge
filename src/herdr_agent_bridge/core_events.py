"""Bounded hc event subscription and connection-local sequence reconciliation."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time

from herdr_agent_bridge.bridge_common import BridgeError


class EventCursor:
    def __init__(self, terminal):
        self.terminal = terminal
        self.stats = {"last_seq": None, "snapshots": 0, "events": 0, "duplicates": 0}

    def new_connection(self):
        self.stats["last_seq"] = None

    def accept(self, message):
        if not isinstance(message, dict):
            raise BridgeError("invalid core event envelope", code="events_invalid")
        if "terminals" in message:
            seq, terminals = message.get("seq"), message["terminals"]
            if (
                type(seq) is not int
                or seq < 0
                or not isinstance(terminals, list)
                or any(
                    not isinstance(item, dict) or item.get("address") != self.terminal
                    for item in terminals
                )
            ):
                raise BridgeError("invalid core event snapshot", code="events_invalid")
            self.stats.update(last_seq=seq, snapshots=self.stats["snapshots"] + 1)
            return True
        params = message.get("params")
        if message.get("jsonrpc") != "2.0" or not isinstance(params, dict):
            raise BridgeError("invalid core event envelope", code="events_invalid")
        seq = params.get("seq")
        if type(seq) is not int or seq < 0:
            raise BridgeError("invalid core event sequence", code="events_invalid")
        if message.get("method") == "missed":
            if self.stats["last_seq"] is not None and seq < self.stats["last_seq"]:
                return False
            return self.accept(params)
        if message.get("method") == "heartbeat":
            return False
        if message.get("method") != "event" or params.get("terminal") != self.terminal:
            raise BridgeError("unexpected core event identity", code="events_invalid")
        if self.stats["last_seq"] is None:
            raise BridgeError("core event arrived before its snapshot", code="events_invalid")
        if seq <= self.stats["last_seq"]:
            self.stats["duplicates"] += 1
            return False
        self.stats.update(last_seq=seq, events=self.stats["events"] + 1)
        return True


class CoreEvents:
    def __init__(self, terminal, env, *, opener=None):
        self.terminal, self.env = terminal, dict(env)
        self.opener = opener or subprocess.Popen
        self.cursor = EventCursor(terminal)
        self.stats = self.cursor.stats
        self.stats.update(connections=0, reconnects=0, state="connecting", pid=None)
        self.selector = selectors.DefaultSelector()
        self.process = None
        self.buffer = b""
        self.retry_at = 0
        self.backoff = 0.25

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _disconnect(self):
        if self.process is not None:
            process, self.process = self.process, None
            self.selector.unregister(process.stdout)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait(timeout=3)
            process.stdout.close()
        self.buffer = b""
        self.stats.update(state="reconnecting", pid=None)
        self.retry_at = time.monotonic() + self.backoff
        self.backoff = min(5, self.backoff * 2)

    def close(self):
        self._disconnect()
        self.selector.close()
        self.stats.update(state="closed", pid=None)

    def read(self, timeout):
        if not 0 < timeout <= 30:
            raise BridgeError(
                "event wait must be between 0 and 30 seconds", code="events_wait_invalid"
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is None:
                delay = self.retry_at - time.monotonic()
                if delay > 0:
                    self.selector.select(min(delay, max(0, deadline - time.monotonic())))
                    continue
                try:
                    self.process = self.opener(
                        ["hc", "events", "--json", "--terminal", self.terminal],
                        env=self.env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        bufsize=0,
                    )
                except OSError:
                    self._disconnect()
                    return True  # Reconcile the bound terminal after connection failure.
                self.cursor.new_connection()
                self.stats["connections"] += 1
                self.stats["reconnects"] = self.stats["connections"] - 1
                self.stats.update(state="connecting", pid=self.process.pid)
                self.selector.register(self.process.stdout, selectors.EVENT_READ)
            selected = self.selector.select(max(0, deadline - time.monotonic()))
            if not selected:
                return False
            chunk = os.read(selected[0][0].fd, 65536)
            if not chunk:
                self._disconnect()
                return True
            self.buffer += chunk
            changed = False
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if len(line) > 1_048_576:
                    raise BridgeError("core event record too large", code="events_invalid")
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise BridgeError("invalid core event JSON", code="events_invalid") from None
                accepted = self.cursor.accept(message)
                changed = accepted or changed
                if accepted:
                    self.stats["state"] = "connected"
                    self.backoff = 0.25
            if len(self.buffer) > 1_048_576:
                raise BridgeError("core event record too large", code="events_invalid")
            if changed:
                return True
        return False
