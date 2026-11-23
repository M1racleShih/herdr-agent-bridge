"""Purpose-based run locations and bounded historical input recognition."""

import re
from pathlib import Path

from .bridge_common import BridgeError

RUNS = Path(".artifacts/runs")
# Existing persisted run pointers only; never a destination for new runs.
LEGACY_RUNS = Path(".artifacts/phase2")


def valid_run_pointer(value, *, legacy=False, project_entry=False):
    if (
        not isinstance(value, str)
        or len(value) > 1024
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        return False
    parts = value.split("/")
    bases = [RUNS.parts]
    if legacy:
        bases.append(LEGACY_RUNS.parts)
    if (
        len(parts) < 3
        or tuple(parts[:2]) not in bases
        or any(part in ("", ".", "..") for part in parts)
    ):
        return False
    return not project_entry or (
        len(parts) == 3 and re.fullmatch(r"[A-Za-z0-9_-]+", parts[2]) is not None
    )


def new_manager_run(root, project):
    project = Path(project).resolve()
    path = Path(root).absolute()
    try:
        pointer = path.relative_to(project).as_posix()
    except ValueError:
        raise BridgeError(
            "run must be a dedicated directory below project/.artifacts/runs"
        ) from None
    if not valid_run_pointer(pointer):
        raise BridgeError("run must be a dedicated directory below project/.artifacts/runs")
    for ancestor in (path, *path.parents):
        if ancestor == project:
            break
        if ancestor.is_symlink():
            raise BridgeError("symlink in new run path")
    if path.resolve() != path:
        raise BridgeError("new run path escapes project")
    return path


def acceptance_directory(project, kind, name):
    if (
        kind not in ("codex", "pi", "core-features")
        or not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None
    ):
        raise BridgeError("unknown acceptance kind")
    path = Path(project).resolve() / ".artifacts" / "acceptance" / kind / name
    for ancestor in (path, *path.parents):
        if ancestor == Path(project).resolve():
            break
        if ancestor.is_symlink():
            raise BridgeError("symlink in acceptance directory")
    path.mkdir(parents=True, mode=0o700, exist_ok=False)
    return path
