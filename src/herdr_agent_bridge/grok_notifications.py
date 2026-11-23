"""Durable, owner-bound outbox for Grok HTTPS and local Hermes Bot Relay.

Transport acceptance is not task success. An interrupted attempt stays unknown
until reconciled and is never retried automatically. Grok acknowledgments remain
caller-supplied associations, not proof of provenance.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import hermes_relay
from .runtime_paths import valid_run_pointer

try:
    from .bridge_common import BridgeError, save
    from .bridge_common import fsync_directory as _sync_directory
except ImportError:  # Also runnable by scripts importing from the scripts directory.
    from herdr_agent_bridge.bridge_common import BridgeError, save
    from herdr_agent_bridge.bridge_common import fsync_directory as _sync_directory


_OWNER_KEYS = frozenset(("bot_id", "conversation_id"))
_REQUIRED_KEYS = frozenset(("event_id", "run_id", "owner", "task_id", "kind"))
_OPTIONAL_KEYS = frozenset(("run", "state", "result", "evidence", "verified_success"))
_MAX_PAYLOAD = 65536
_MAX_CONFIG = 65536
_TIMEOUT = 15


def _identity(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 256
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def validate_owner(value):
    if (
        not isinstance(value, dict)
        or set(value) != _OWNER_KEYS
        or not all(_identity(value[k]) for k in _OWNER_KEYS)
    ):
        raise BridgeError("owner must contain bot_id and conversation_id strings")
    for item in value.values():
        normalized = item.strip().casefold()
        header = normalized.split(":", 1)[0].strip()
        words = normalized.split(None, 1)
        if (
            len(words) == 2
            and words[0] == "bearer"
            or ":" in normalized
            and header in ("authorization", "x-api-key", "api-key", "api_key")
        ):
            raise BridgeError("owner identifiers must not contain authentication headers")
    return {key: value[key] for key in ("bot_id", "conversation_id")}


def _credential_free(value, key):
    """Check decoded JSON, including escaped strings and object keys."""
    if key is None:
        return
    if isinstance(value, str):
        if key in value:
            raise BridgeError(
                "credential material is not allowed in identifiers or notification data"
            )
    elif isinstance(value, dict):
        for name, item in value.items():
            _credential_free(name, key)
            _credential_free(item, key)
    elif isinstance(value, list):
        for item in value:
            _credential_free(item, key)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _url(value, allow_test_loopback):
    # Reject all query strings because they can contain secrets. Control characters
    # are checked before urlsplit, which may otherwise discard them.
    if (
        not isinstance(value, str)
        or not value
        or "?" in value
        or "#" in value
        or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
    ):
        raise BridgeError("invalid notification endpoint")
    try:
        parsed = urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        raise BridgeError("invalid notification endpoint") from None
    if (
        not hostname
        or "@" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port == 0)
    ):
        raise BridgeError("invalid notification endpoint")
    if parsed.scheme == "https":
        return
    is_loopback = hostname.lower() == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
    if parsed.scheme != "http" or not allow_test_loopback or not is_loopback:
        raise BridgeError("notification endpoint must use HTTPS")


def _load_config(path, owner, allow_test_loopback=False):
    owner = validate_owner(owner)
    # Open without following the final symlink, and check the opened descriptor
    # rather than a path that could be replaced between stat and read.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise BridgeError("notification config must be a private regular file")
            raw = stream.read(_MAX_CONFIG + 1)
    except BridgeError:
        raise
    except (OSError, TypeError, ValueError):
        raise BridgeError("cannot read private notification config") from None
    if len(raw) > _MAX_CONFIG:
        raise BridgeError("invalid notification config")
    try:
        config = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise BridgeError("invalid notification config") from None
    if not isinstance(config, dict) or not _OWNER_KEYS.issubset(config):
        raise BridgeError("invalid notification config")
    if validate_owner({key: config[key] for key in _OWNER_KEYS}) != owner:
        raise BridgeError("notification config owner does not match")
    if config.get("transport") == hermes_relay.TRANSPORT:
        if info.st_uid != os.geteuid():
            raise BridgeError("Hermes Relay config must belong to the current user")
        try:
            hermes_relay.validate_config(config, owner)
        except ValueError as exc:
            raise BridgeError(str(exc)) from None
        return config
    if set(config) != _OWNER_KEYS | {"url", "key"}:
        raise BridgeError("invalid notification config")
    _url(config["url"], allow_test_loopback)
    key = config["key"]
    if (
        not isinstance(key, str)
        or not key
        or len(key) > 8192
        or any(ord(c) <= 32 or ord(c) >= 127 for c in key)
    ):
        raise BridgeError("invalid notification credential")
    _credential_free(owner, key)
    return config


def load_config(path, owner):
    """Validate a private notification config; it may contain secrets.

    Callers must not print or log the return value. Plain HTTP is allowed
    solely inside deliver() when a test sender is explicitly injected.
    """
    return _load_config(path, owner)


def _json_value(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise BridgeError("notification payload must contain JSON values")


def _payload(value, owner):
    if (
        not isinstance(value, dict)
        or not _REQUIRED_KEYS.issubset(value)
        or set(value) - _REQUIRED_KEYS - _OPTIONAL_KEYS
    ):
        raise BridgeError("invalid notification payload fields")
    if validate_owner(value["owner"]) != owner:
        raise BridgeError("notification payload owner does not match")
    if not all(_identity(value[key]) for key in ("event_id", "run_id", "kind")):
        raise BridgeError("invalid notification payload identifiers")
    if not (
        _identity(value["task_id"])
        or value["task_id"] is None
        and value["kind"]
        in ("permission_required", "run_ready", "agent_error", "exited", "recovery_required")
    ):
        raise BridgeError("invalid notification task association")
    if "run" in value:
        # Legacy pointers remain valid in existing outbox payloads and callbacks.
        if not valid_run_pointer(value["run"], legacy=True):
            raise BridgeError("invalid notification run pointer")
    if "verified_success" in value and not isinstance(value["verified_success"], bool):
        raise BridgeError("verified_success must be boolean")
    try:
        _json_value(value)
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > _MAX_PAYLOAD:
            raise BridgeError("notification payload exceeds size limit")
        # Keep an independent JSON copy so caller mutation cannot affect records.
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise BridgeError("notification payload must contain JSON values") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url, key, body):
    """POST once, without following redirects or retaining response content."""
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=_TIMEOUT) as response:
            return response.status
    except HTTPError as exc:
        # Includes redirects refused by _NoRedirect; do not read their bodies.
        try:
            return exc.code
        finally:
            exc.close()


class Outbox:
    def __init__(self, root, owner, credential=None):
        self.root = Path(root).resolve()
        self.owner = validate_owner(owner)
        self.credential = credential
        _credential_free(self.owner, credential)
        missing = []
        parent = self.root
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            _sync_directory(directory.parent)
        self.path = self.root / "notifications.json"
        self.lock_path = self.root / "notifications.lock"
        with self._locked() as data:
            if data is None:
                self._save({"format": 1, "owner": self.owner, "events": {}})

    @contextmanager
    def _locked(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags, 0o600)
            with os.fdopen(fd, "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    data = None
                if data is not None:
                    if (
                        not isinstance(data, dict)
                        or data.get("format") != 1
                        or data.get("owner") != self.owner
                        or not isinstance(data.get("events"), dict)
                    ):
                        raise BridgeError("notification journal owner or format mismatch")
                yield data
        except BridgeError:
            raise

    def _save(self, data):
        _credential_free(data, self.credential)
        save(self.path, data)
        # The rename itself must be durable before initiating the network call.
        _sync_directory(self.root)

    def _event(self, data, event_id):
        if not _identity(event_id):
            raise BridgeError("invalid notification event id")
        try:
            event = data["events"][event_id]
        except KeyError:
            raise BridgeError("unknown notification event id") from None
        if not isinstance(event, dict):
            raise BridgeError("invalid notification event record")
        payload = _payload(event.get("payload"), self.owner)
        if payload["event_id"] != event_id:
            raise BridgeError("notification event association mismatch")
        _credential_free(event, self.credential)
        return event

    def enqueue(self, payload):
        payload = _payload(payload, self.owner)
        _credential_free(payload, self.credential)
        with self._locked() as data:
            event_id = payload["event_id"]
            previous = data["events"].get(event_id)
            if previous is not None:
                previous = self._event(data, event_id)
                # Canonical JSON distinguishes, for example, 1 from true.
                if json.dumps(
                    previous["payload"],
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ) != json.dumps(
                    payload,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ):
                    raise BridgeError("notification event id already has different payload")
                return json.loads(json.dumps(previous))
            event = {
                "payload": payload,
                "state": "pending",
                "attempts": [],
                "acknowledgment": None,
                "created_at": time.time(),
            }
            data["events"][event_id] = event
            self._save(data)
            return json.loads(json.dumps(event))

    def get(self, event_id):
        with self._locked() as data:
            return json.loads(json.dumps(self._event(data, event_id)))

    def deliver(self, event_id, config_path, sender=None, retry=False, *, relay_context=None):
        # Reject invalid config before touching the delivery journal.
        config = _load_config(config_path, self.owner, allow_test_loopback=sender is not None)
        is_relay = config.get("transport") == hermes_relay.TRANSPORT
        if is_relay and sender is not None:
            raise BridgeError("Hermes Relay does not accept a webhook sender override")
        with self._locked() as data:
            event = self._event(data, event_id)
            _credential_free(event, config.get("key"))
            if not is_relay and "relay" in event:
                raise BridgeError("notification transport changed; reconcile the original event")
            route = self._relay_pointer(event, relay_context) if is_relay else None
            state = event["state"]
            if state == "pending":
                if retry:
                    raise BridgeError("retry is only allowed for a rejected notification")
            elif state != "rejected" or not retry:
                raise BridgeError("notification is not eligible for delivery")
            attempt = {"started_at": time.time()}
            event["state"] = "unknown"
            event["attempts"].append(attempt)
            if route is not None:
                event["relay"] = {
                    "delivery_id": hermes_relay.delivery_id(route),
                    "status": "unknown",
                }
            self._save(data)  # A crash or lost reply leaves unknown on disk.
            if route is not None:
                status = hermes_relay.dispatch(config, route)
                event["relay"]["status"] = attempt["relay_status"] = status
                if status in hermes_relay.ADMITTED:
                    event["state"] = "accepted"
                elif status == "not_sent":
                    event["state"] = "rejected"
            else:
                self._deliver_webhook(event, config, _post if sender is None else sender, attempt)
            attempt["finished_at"] = time.time()
            self._save(data)
            return json.loads(json.dumps(event))

    @staticmethod
    def _relay_pointer(event, relay_context):
        try:
            route = hermes_relay.pointer(event["payload"], relay_context)
        except ValueError as exc:
            raise BridgeError(str(exc)) from None
        prior = event.get("relay")
        if prior is not None and prior.get("delivery_id") != hermes_relay.delivery_id(route):
            raise BridgeError("Hermes Relay delivery identity changed")
        return route

    def reconcile_relay(self, event_id, config_path, relay_context):
        config = load_config(config_path, self.owner)
        if config.get("transport") != hermes_relay.TRANSPORT:
            raise BridgeError("notification is not bound to Hermes Relay")
        with self._locked() as data:
            event = self._event(data, event_id)
            if event["state"] in ("unknown", "accepted") and "relay" in event:
                route = self._relay_pointer(event, relay_context)
                status = hermes_relay.dispatch(config, route, inspect=True)
                if status in hermes_relay.ADMITTED:
                    event["state"] = "accepted"
                    event["relay"]["status"] = status
                    self._save(data)
            return json.loads(json.dumps(event))

    @staticmethod
    def _deliver_webhook(event, config, send, attempt):
        body = json.dumps(
            event["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        try:
            status = send(config["url"], config["key"], body)
            if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
                attempt["error_type"] = "invalid_status"
            else:
                attempt["status_code"] = int(status)
                if status == 200:
                    event["state"] = "accepted"
                elif 300 <= status < 500 and status != 408:
                    event["state"] = "rejected"
                # A server/proxy error can arrive after the routine started.
                # Non-contract success replies and request timeouts are also
                # ambiguous: preserve unknown and never offer a replay.
        except Exception as exc:
            # Never persist URLs, credentials, exception messages or response bodies.
            attempt["error_type"] = (
                "timeout" if isinstance(exc, TimeoutError) else "transport_error"
            )

    def acknowledge(self, event_id, owner, receipt):
        if validate_owner(owner) != self.owner:
            raise BridgeError("notification acknowledgment owner does not match")
        if not _identity(receipt):
            raise BridgeError("invalid notification acknowledgment receipt")
        _credential_free(receipt, self.credential)
        with self._locked() as data:
            event = self._event(data, event_id)
            if event["state"] not in ("unknown", "accepted"):
                raise BridgeError("notification has no delivery to acknowledge")
            existing = event["acknowledgment"]
            if existing is not None:
                if existing["receipt"] != receipt:
                    raise BridgeError("notification already has a different acknowledgment")
                return json.loads(json.dumps(event))
            if any(
                other.get("acknowledgment") is not None
                and other["acknowledgment"]["receipt"] == receipt
                for key, other in data["events"].items()
                if key != event_id
            ):
                raise BridgeError("acknowledgment receipt is bound to another event")
            event["acknowledgment"] = {
                "owner": self.owner,
                "receipt": receipt,
                "acknowledged_at": time.time(),
            }
            self._save(data)
            return json.loads(json.dumps(event))
