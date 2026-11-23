"""Private bootstrap for one hc-backed manager watcher.

Only the run path and random monitor token are arguments. The owner and webhook
configuration are read from the run's private manager journal by Manager.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from herdr_agent_bridge.grok_bridge import Manager


def main():
    if len(sys.argv) != 3:
        return 2
    root, token = Path(sys.argv[1]).resolve(), sys.argv[2]
    # Redirect before reading private state or reporting any watcher exception.
    fd = os.open(
        root / "monitor.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)
    data = json.loads((root / "manager.json").read_text())
    monitor = data.get("monitor") or {}
    if monitor.get("token") != token or monitor.get("backend") != "hc":
        raise RuntimeError("monitor bootstrap identity mismatch")
    Manager(root, data["owner"]).watch(monitor["seconds"], token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
