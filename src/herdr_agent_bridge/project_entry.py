"""Reusable adapter for a Bot's fixed, private project configuration.

A local launcher supplies the config path; task IDs, prompts and commands come
from the caller and are validated by project_bridge. The audit records process
provenance only, never arguments, prompts, output or credentials.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

from herdr_agent_bridge.bridge_common import save
from herdr_agent_bridge.project_bridge import main as project_main

_OPERATIONS = frozenset(
    """init config runtime app-space defaults start status result submit
    verify decide close ack notify monitor takeover resume reconcile notification
    interrupt native-command recovery-status recover""".split()
)


def ancestry():
    result, pid = [], os.getpid()
    for _ in range(16):
        try:
            value = (Path("/proc") / str(pid) / "stat").read_text()
            parent = int(value.rsplit(")", 1)[1].split()[1])
            result.append(
                {"pid": pid, "parent": parent, "name": value.split("(", 1)[1].rsplit(")", 1)[0]}
            )
            if parent <= 0 or parent == pid:
                break
            pid = parent
        except (OSError, ValueError, IndexError):
            break
    return result


def run_bound(config, argv=None, *, audit_dir=None):
    """Run the full project CLI without allowing the caller to replace its binding."""
    config = Path(config).absolute()
    args = list(sys.argv[1:] if argv is None else argv)
    directory = Path(audit_dir) if audit_dir is not None else config.parent / "entry-calls"
    record = {
        "invocation_id": secrets.token_hex(12),
        "operation": args[0] if args and args[0] in _OPERATIONS else "unrecognized",
        "phase": "attempted",
        "at": time.time(),
        "ancestry": ancestry(),
    }
    path = directory / (record["invocation_id"] + ".json")
    try:
        if directory.is_symlink():
            raise OSError("audit directory is a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        save(path, record)  # Intent precedes every possible project side effect.
    except OSError:
        print(
            json.dumps({"error": "entry_audit_unavailable", "delivery": "not_attempted"}),
            file=sys.stderr,
        )
        return 1
    try:
        code = project_main(args, bound_config=config)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except (Exception, KeyboardInterrupt):
        record.update(phase="delivery_unknown", finished_at=time.time())
        try:
            save(path, record)
        except OSError:
            pass
        print(
            json.dumps(
                {
                    "error": "entry_operation_interrupted",
                    "delivery": "unknown",
                    "action": "inspect the original request; do not replay start or submit",
                }
            ),
            file=sys.stderr,
        )
        return 1
    record.update(phase="returned", returncode=code, finished_at=time.time())
    try:
        save(path, record)
    except OSError:
        # The operation already ran; failing to record its return does not undo it.
        print(
            json.dumps(
                {
                    "error": "entry_audit_return_unavailable",
                    "delivery": "unknown",
                    "action": "inspect the original request; do not replay start or submit",
                }
            ),
            file=sys.stderr,
        )
        return 1
    return code
