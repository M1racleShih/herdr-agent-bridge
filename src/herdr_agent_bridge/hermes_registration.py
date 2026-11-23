"""Private operator worker for the opt-in Hermes registration slash command.

This process is called by the Hermes plugin under the local operator account.
It is not an agent tool or a proof of human consent by itself; only the plugin
links a commit to Hermes's authenticated viewer confirmation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from herdr_agent_bridge.bridge_common import BridgeError
from herdr_agent_bridge.grok_notifications import _no_duplicate_keys
from herdr_agent_bridge.personal_entry import _load_binding

MAX_REQUEST = 32768


def handle(binding, request):
    if not isinstance(request, dict) or not isinstance(request.get("owner"), dict):
        raise BridgeError("invalid registration message")
    registry, owner, _ = _load_binding(binding)
    if request["owner"] != owner:
        raise BridgeError("registration owner does not match original binding")
    if request.keys() == {"operation", "owner", "selector"} and request["operation"] == "prepare":
        result = registry.prepare_registration(request["selector"])
        if result["status"] == "needs_confirmation":
            return {
                "status": "needs_confirmation",
                "proposal": {
                    "root": result["root"],
                    "identity": result["directory_identity"],
                    "registry": str(registry.path),
                },
            }
        matches = result.get("matches", [])
        return {"status": result["status"], "matches": matches[:8], "more": len(matches) > 8}
    if request.keys() != {"operation", "owner", "proposal"} or request["operation"] != "commit":
        raise BridgeError("invalid registration operation")
    proposal = request["proposal"]
    if (
        not isinstance(proposal, dict)
        or proposal.keys() != {"root", "identity", "registry"}
        or proposal["registry"] != str(registry.path)
        or not isinstance(proposal["root"], str)
        or not isinstance(proposal["identity"], list)
        or len(proposal["identity"]) != 2
        or any(type(value) is not int or value < 0 for value in proposal["identity"])
    ):
        raise BridgeError("registration proposal no longer matches its binding")
    entry = registry.register_confirmed(
        proposal["root"], expected_directory=tuple(proposal["identity"])
    )
    return {"status": "registered", "entry": entry}


def main():
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_absolute():
        return 2
    request = None
    try:
        body = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(body) > MAX_REQUEST:
            raise ValueError("registration message too large")
        request = json.loads(body, object_pairs_hook=_no_duplicate_keys)
        result = handle(Path(sys.argv[1]), request)
    except (BridgeError, ValueError, TypeError, KeyError, RecursionError):
        result = {"status": "refused"}
    except OSError:
        attempted = isinstance(request, dict) and request.get("operation") == "commit"
        result = {"status": "unknown" if attempted else "refused"}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
