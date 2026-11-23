"""Compatibility entry; install herdr-agent-bridge before use."""

import sys

from herdr_agent_bridge import pi_evidence as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    # Preserve registered Python wrappers (not a second implementation).
    sys.modules[__name__] = _implementation
