"""Independent local/CI quality gates; run from the repository root."""

import argparse
import subprocess
import sys

COMMANDS = {
    "lint": ["ruff", "check", "."],
    "format": ["ruff", "format", "--check", "."],
    "types": ["mypy"],
    "naming": [sys.executable, "-B", "tools/check_naming.py"],
    "test": [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("gate", choices=[*COMMANDS, "all"])
args = parser.parse_args()
for name in COMMANDS if args.gate == "all" else [args.gate]:
    result = subprocess.run(COMMANDS[name], check=False)
    if result.returncode:
        sys.exit(result.returncode)
