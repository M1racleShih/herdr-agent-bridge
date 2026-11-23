"""Shared persistence and hc transport primitives, independent of execution."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import site
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict

# Pin the launching interpreter user site before any child changes HOME or cwd.
PYTHON_USER_BASE = str(Path(site.getuserbase()).absolute())
PYTHON_NO_USER_SITE = "" if site.ENABLE_USER_SITE else "1"


class HcCall(TypedDict):
    args: list[str]
    exit_code: int
    stdout: str
    stderr: str


def save(path: str | Path, value: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def fsync_directory(path: str | Path) -> None:
    """Make an atomic journal rename durable before a side effect."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class BridgeError(RuntimeError):
    def __init__(self, *args: object, code: str = "bridge_operation_failed") -> None:
        self.code = code
        super().__init__(*args)


class HcError(BridgeError):
    def __init__(self, call: HcCall) -> None:
        self.call = call
        super().__init__(json.dumps(call, ensure_ascii=False))


def hc_error_kind(exc: HcError) -> str | None:
    try:
        value = json.loads(exc.call["stderr"])
        error = value.get("error") if isinstance(value, dict) else None
        kind = error.get("kind") if isinstance(error, dict) else None
        return kind if isinstance(kind, str) else None
    except (ValueError, KeyError, TypeError):
        return None


def run_hc(args: Sequence[str], env: Mapping[str, str], timeout: float) -> object:
    result = subprocess.run(["hc", *args], env=env, capture_output=True, text=True, timeout=timeout)
    call: HcCall = {
        "args": list(args),
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if result.returncode:
        raise HcError(call)
    return json.loads(result.stdout)
