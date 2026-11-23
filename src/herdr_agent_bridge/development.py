"""Scoped development contracts and independent final-worktree evidence.

These controls are an operator contract, not an OS sandbox. Authorized checks
execute project code with the current account's permissions.
"""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from herdr_agent_bridge.bridge_common import BridgeError


class VerificationBusy(BridgeError):
    def __init__(self):
        super().__init__(
            "verification process still active; inspect the original verification",
            code="verification_in_progress",
        )


@contextmanager
def verification_lease(root):
    """Keep exclusive check ownership, including across a parent crash."""
    fd = os.open(Path(root) / "verification.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise VerificationBusy() from None
        # The check inherits this open description, so a dead parent cannot
        # make its still-running check look safe to repeat or clean up.
        yield stream


def sha(value):
    return hashlib.sha256(value).hexdigest()


def safe_file(project, relative):
    if not isinstance(relative, str):
        raise ValueError("expected a project-relative file path")
    path = Path(relative)
    if (
        not isinstance(relative, str)
        or not relative
        or path.is_absolute()
        or any(part in (".", "..", ".git") for part in path.parts)
        or str(path) != relative
    ):
        raise ValueError("expected a canonical project-relative file path")
    target = Path(project) / path
    for item in (target, *target.parents):
        if item == Path(project):
            break
        if item.is_symlink():
            raise ValueError("symlink in development path")
    if not target.resolve().is_relative_to(Path(project).resolve()):
        raise ValueError("path outside project")
    if target.exists() and not target.is_file():
        raise ValueError("development path is not a file")
    return target


def snapshot(project, extra=()):
    result = subprocess.run(
        ["git", "-C", str(project), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    )
    names = set(os.fsdecode(name) for name in result.stdout.split(b"\0") if name)
    names.update(extra)
    records = {}
    for name in sorted(names):
        # Generated evidence and Python caches are not source changes.
        if name.startswith(".artifacts/") or "__pycache__" in Path(name).parts:
            if name not in extra:
                continue
        path = Path(project) / name
        if path.is_symlink():
            records[name] = {"link": os.readlink(path)}
        elif path.is_file():
            records[name] = {"sha256": sha(path.read_bytes()), "mode": path.stat().st_mode & 0o777}
        elif not path.exists():
            records[name] = None
        else:
            raise ValueError("unsupported source entry")
    return records


def fingerprint(records):
    return sha(json.dumps(records, sort_keys=True).encode())


def prepare(project, spec):
    if not isinstance(spec, dict) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,100}", spec.get("task_id", "")
    ):
        raise ValueError("invalid development task identifier")
    for key in ("goal",):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise ValueError("missing development goal")
    paths = spec.get("allowed_paths")
    if not isinstance(paths, list) or not paths or len(set(paths)) != len(paths):
        raise ValueError("explicit unique writable files required")
    contents = {}
    for name in paths:
        path = safe_file(project, name)
        contents[name] = path.read_text() if path.exists() else None
    criteria = spec.get("acceptance")
    if (
        not isinstance(criteria, list)
        or not criteria
        or not all(isinstance(x, str) and x.strip() for x in criteria)
    ):
        raise ValueError("explicit acceptance criteria required")
    checks = spec.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("explicit checks required")
    names = set()
    for check in checks:
        if (
            not isinstance(check, dict)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", check.get("name", ""))
            or check["name"] in names
            or not isinstance(check.get("argv"), list)
            or not check["argv"]
            or not all(isinstance(x, str) and x and "\0" not in x for x in check["argv"])
            or type(check.get("timeout")) is not int
            or not 1 <= check["timeout"] <= 120
        ):
            raise ValueError("invalid named check")
        names.add(check["name"])
    return {
        **spec,
        "baseline": snapshot(project, paths),
        "original_contents": contents,
        "created_at": time.time(),
    }


def _stop_group(process):
    if process is None:
        return False
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False


def collect(project, contract, *, cancelled=None, lease_fd=None):
    before = snapshot(project, contract["allowed_paths"])
    checks = []
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for check in contract["checks"]:
        started = time.time()
        was_cancelled = False
        left_descendants = False
        with tempfile.TemporaryFile() as output:
            process = None
            try:
                if cancelled is not None and cancelled():
                    code, timed_out, was_cancelled = None, False, True
                else:
                    process = subprocess.Popen(
                        check["argv"],
                        cwd=project,
                        env=env,
                        start_new_session=True,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        pass_fds=() if lease_fd is None else (lease_fd,),
                    )
                    deadline = time.monotonic() + check["timeout"]
                    while True:
                        if cancelled is not None and cancelled():
                            _stop_group(process)
                            process.wait()
                            code, timed_out, was_cancelled = None, False, True
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(check["argv"], check["timeout"])
                        try:
                            code = process.wait(
                                timeout=remaining if cancelled is None else min(0.1, remaining)
                            )
                            timed_out = False
                            break
                        except subprocess.TimeoutExpired:
                            if cancelled is None:
                                raise
            except subprocess.TimeoutExpired:
                _stop_group(process)
                process.wait()
                code, timed_out = None, True
            except BaseException:
                if process is not None:
                    _stop_group(process)
                    process.wait()
                raise
            if process is not None and not timed_out and not was_cancelled:
                left_descendants = _stop_group(process)
            output.seek(0)
            data = output.read(1_048_577)
        checks.append(
            {
                **check,
                "started_at": started,
                "finished_at": time.time(),
                "exit_code": code,
                "timed_out": timed_out,
                "cancelled": was_cancelled,
                "left_descendants": left_descendants,
                "output": data[:1_048_576].decode(errors="replace"),
                "output_truncated": len(data) > 1_048_576,
                "passed": code == 0
                and not timed_out
                and not was_cancelled
                and not left_descendants,
            }
        )
        if was_cancelled or left_descendants:
            break
    after = snapshot(project, contract["allowed_paths"])
    baseline = contract["baseline"]
    changed = sorted(
        name for name in baseline.keys() | after.keys() if baseline.get(name) != after.get(name)
    )
    outside = sorted(set(changed) - set(contract["allowed_paths"]))
    diff = []
    for name, original in contract["original_contents"].items():
        path = safe_file(project, name)
        final = path.read_text() if path.exists() else ""
        diff.extend(
            difflib.unified_diff(
                (original or "").splitlines(True),
                final.splitlines(True),
                fromfile="baseline/" + name,
                tofile="final/" + name,
            )
        )
    stable = before == after
    result = {
        "passed": bool(changed) and not outside and stable and all(c["passed"] for c in checks),
        "changed_paths": changed,
        "outside_scope": outside,
        "stable_during_checks": stable,
        "worktree_sha256": fingerprint(after),
        "checks": checks,
        "diff": "".join(diff),
        "collected_at": time.time(),
    }
    if any(check["cancelled"] for check in checks):
        result.update(passed=False, reason="verification_cancelled")
    elif any(check["left_descendants"] for check in checks):
        result.update(passed=False, reason="check_left_descendants")
    return result
