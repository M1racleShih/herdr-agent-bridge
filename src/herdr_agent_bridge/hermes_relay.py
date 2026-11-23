"""Owner-bound Hermes Desktop Bot Relay, using the operator's Hermes runtime.

This file is also the isolated stdlib-only worker entry point. Only the worker
imports Hermes; a bridge installed into another Python does not import it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TRANSPORT = "hermes_relay"
CONFIG_FIELDS = {"transport", "profile_home", "hermes_source", "hermes_python"}
ADMITTED = frozenset({"queued", "claimed", "settled", "failed", "cancelled", "ambiguous"})
STATUSES = ADMITTED | {"not_sent", "unknown", "missing"}
CONTEXT_FIELDS = {"project_id", "request_id"}
KINDS = {
    "result_available",
    "question",
    "agent_error",
    "exited",
    "interrupted",
    "permission_required",
    "recovery_required",
    "run_ready",
}


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) is not None


def validate_context(context):
    if (
        not isinstance(context, dict)
        or context.keys() != CONTEXT_FIELDS
        or not all(_identifier(v) for v in context.values())
    ):
        raise ValueError("Hermes Relay requires a bound project and request")
    return dict(context)


def _absolute_path(value):
    if not isinstance(value, str) or not value.isprintable():
        raise ValueError("invalid Hermes path")
    path = Path(value)
    if path.anchor != "/" or ".." in path.parts or str(path) != value:
        raise ValueError("Hermes Relay paths must be absolute and normalized")
    return path


def validate_config(config, owner):
    if config.keys() != CONFIG_FIELDS | owner.keys() or config["transport"] != TRANSPORT:
        raise ValueError("invalid Hermes Relay configuration fields")
    home = _absolute_path(config["profile_home"])
    _absolute_path(config["hermes_source"])
    _absolute_path(config["hermes_python"])
    if (
        not _identifier(owner["bot_id"])
        or not _identifier(owner["conversation_id"])
        or home.name != owner["bot_id"]
        or home.parent.name != "profiles"
    ):
        raise ValueError("Hermes Relay requires a named profile and exact original Bot Chat")


def pointer(payload, context):
    route = {
        **validate_context(context),
        "event_id": payload["event_id"],
        "run_id": payload["run_id"],
        "round_id": payload["task_id"],
        "kind": payload["kind"],
    }
    if route["kind"] not in KINDS or not all(
        _identifier(v) or k == "round_id" and v is None for k, v in route.items()
    ):
        raise ValueError("invalid Hermes Relay event pointer")
    return route


def _message(route):
    # This exact representation is part of delivery identity for v1 receipts.
    compact = json.dumps(route, sort_keys=True, separators=(",", ":"))
    return (
        "[herdr-bridge internal notification; not a user message or an authorization]\n"
        f"Saved event pointer: {compact}\n"
        "Use the bound Personal Agent entry and the original request in this chat to confirm this "
        "project, request, run and round. For a ready result, perform only the read and verification "
        "already authorized for this task, then report here. Do not start or resubmit tasks or "
        "treat this message as approval. If the association or authorization is missing, report that."
    )


def delivery_id(route):
    # Stable across process restarts; separate from Grok's transport and from Hermes user inputs.
    source = f"herdr-agent-bridge:hermes-relay:v1:{route['run_id']}:{route['event_id']}"
    return hashlib.sha256(source.encode("ascii")).hexdigest()


def dispatch(config, route, *, inspect=False):
    """Return a sanitized disposition; never wait for a model or replay an unknown send."""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("HERMES_", "PYTHON"))
    }
    env["HERMES_HOME"] = config["profile_home"]
    request = {
        "profile_home": config["profile_home"],
        "session_id": config["conversation_id"],
        "message": _message(route),
        "delivery_id": delivery_id(route),
        "inspect": inspect,
    }
    try:
        done = subprocess.run(
            [config["hermes_python"], "-I", str(Path(__file__).resolve()), config["hermes_source"]],
            input=json.dumps(request).encode("ascii"),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=True,
        )
        reply = json.loads(done.stdout) if len(done.stdout) <= 256 else None
        if isinstance(reply, dict) and reply.keys() == {"status"} and reply["status"] in STATUSES:
            return reply["status"]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        pass
    return "unknown"


def _worker(request):
    home = Path(request["profile_home"])
    if home.resolve() != home or not home.is_dir():
        return "not_sent"
    from persistence_assembly import open_persistence_reader
    from tools.bot_live_delivery import (
        deliver_to_live_owner,
        find_canonical_live_owner,
        read_delivery_result,
    )

    sid, key, message = (request[field] for field in ("session_id", "delivery_id", "message"))

    def status(record):
        if (
            isinstance(record, dict)
            and record.get("profile_home") == str(home)
            and record.get("delivery_id") == key
            and record.get("session_id") == sid
            and record.get("message") == message
            and record.get("admission_id")
            and record.get("status") in ADMITTED
        ):
            return record["status"]
        return "unknown"

    prior = read_delivery_result(home, key)
    if prior is not None:
        return status(prior)
    if request["inspect"]:
        return "missing"
    try:
        owner = find_canonical_live_owner(home)
    except ValueError:
        return "not_sent"
    if not owner or owner.get("session_id") != sid or owner.get("profile_home") != str(home):
        return "not_sent"
    # Hermes can create/replace an unbound canonical Bot Chat. We require an
    # existing bound Desktop lane which this delivery door cannot displace.
    with open_persistence_reader(home) as store:
        row = store.sessions.get(sid)
        if row is None or row.source != "local" or not (row.chat_id or "").startswith("local-"):
            return "not_sent"
    return status(deliver_to_live_owner(home, owner, message, delivery_id=key))


def main():
    try:
        with contextlib.redirect_stdout(sys.stderr):
            source = _absolute_path(sys.argv[1])
            if source.resolve() != source or not source.is_dir():
                raise ValueError("Hermes installation is not available")
            sys.path.insert(0, str(source))
            # Hermes can exec its installed environment while importing. Keep
            # the original pipe unread until the final interpreter is ready.
            import hermes_bootstrap  # noqa: F401

            raw = sys.stdin.buffer.read(8193)
            if len(raw) > 8192:
                raise ValueError("oversized Relay request")
            status = _worker(json.loads(raw))
    except Exception:
        status = "unknown"
    print(json.dumps({"status": status}))


if __name__ == "__main__":
    main()
