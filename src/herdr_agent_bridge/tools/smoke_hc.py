"""Check the installed hc using an inert program in a disposable session."""

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def require(condition, message):
    if not condition:
        raise RuntimeError(str(message))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".artifacts/hc-smoke/results.json"))
    args = parser.parse_args(argv)
    if os.name != "posix" or sys.platform not in {"linux", "darwin"}:
        parser.error("this smoke check currently supports Linux and macOS only")
    if shutil.which("hc") is None:
        parser.error("hc is not installed or not on PATH")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    scratch = Path(tempfile.mkdtemp(prefix="hab-hc-", dir="/tmp"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("HC_")}
    for key, name in [
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_RUNTIME_DIR", "run"),
        ("XDG_CACHE_HOME", "cache"),
    ]:
        folder = scratch / name
        folder.mkdir(mode=0o700)
        env[key] = str(folder)
    env["HC_SESSION"] = "smoke-" + secrets.token_hex(8)
    fixture = scratch / "fixture.py"
    fixture.write_text("""import json, sys
def status(s):
    print("\\x1b]7501;state=" + s + ":app=bridge-fixture\\x1b\\\\", flush=True)
status("idle")
for line in sys.stdin:
    task = line.strip()
    if task == "block":
        status("blocked")
        continue
    status("working")
    print("BRIDGE_RESULT " + json.dumps({"task_id": task, "evidence": "local fixture"}), flush=True)
    status("done")
""")
    calls, checks = [], []
    result = {
        "scope": "installed hc; isolated session; inert local fixture, no model or external service",
        "checks": checks,
        "calls": calls,
        "passed": False,
        "phase": "running",
        "scratch": str(scratch),
        "session": env["HC_SESSION"],
        "not_verified": ["real coding agent", "Grok Bot", "Herdr TUI", "webhook"],
    }

    def write_report():
        temporary = args.output.with_name(args.output.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            os.replace(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)

    try:
        write_report()
    except Exception:
        shutil.rmtree(scratch)
        raise

    def hc(*argv, check=True):
        p = subprocess.run(
            ["hc", *argv], env=env, cwd=scratch, capture_output=True, text=True, timeout=15
        )
        calls.append(
            {"argv": list(argv), "exit_code": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
        )
        if check and p.returncode:
            raise RuntimeError(f"hc {argv}: {p.stderr}")
        return p

    failure = None
    completed = False
    previous_term = signal.getsignal(signal.SIGTERM)
    received_signals = []

    def interrupted(signum, frame):
        received_signals.append(signum)
        # InterruptedError may be retried inside subprocess/selectors.
        raise RuntimeError(f"received signal {signum}")

    def record_interruption(signum, frame):
        received_signals.append(signum)

    try:
        signal.signal(signal.SIGTERM, interrupted)
        result["hc_version"] = hc("--version").stdout.strip()
        hc(
            "run",
            "--bg",
            "--json",
            "--name",
            "bridge-smoke",
            "--cwd",
            str(scratch),
            sys.executable,
            str(fixture),
        )
        ready = json.loads(hc("wait", "bridge-smoke", "--timeout", "5").stdout)
        require(ready.get("status") == "idle", {"expected": "idle", "observed": ready})
        turn = ready["turn"]
        for task in ["task-001", "followup-001"]:
            sent = json.loads(hc("send", "--wait", "--timeout", "5", "bridge-smoke", task).stdout)
            waited = sent.get("wait", {})
            require(sent.get("started") and waited.get("status") == "done", sent)
            require(waited["turn"] > turn, {"error": "turn did not advance", "observed": waited})
            turn = waited["turn"]
            screen = hc("read", "bridge-smoke", "--lines", "30").stdout
            evidence = [
                json.loads(line.removeprefix("BRIDGE_RESULT "))
                for line in screen.splitlines()
                if line.startswith("BRIDGE_RESULT ")
            ]
            require(
                any(item.get("task_id") == task for item in evidence),
                {"error": "missing task evidence", "task_id": task, "observed": evidence},
            )
            checks.append({"check": task, "passed": True})
        hc("send", "--wait", "--timeout", "5", "bridge-smoke", "block")
        state = json.loads(hc("get", "bridge-smoke").stdout)
        require(state.get("status", {}).get("state") == "blocked", state)
        refused = hc("send", "bridge-smoke", "must-not-arrive", check=False)
        require(refused.returncode != 0, "blocked send unexpectedly succeeded")
        require(
            json.loads(refused.stderr).get("error", {}).get("kind") == "blocked", refused.stderr
        )
        checks.append({"check": "blocked_send_refused", "passed": True})
        completed = True
    except (Exception, KeyboardInterrupt) as exc:
        failure = f"{type(exc).__name__}: {exc}"
        result["error"] = failure
    finally:
        signal.signal(signal.SIGTERM, record_interruption)
        cleanup = []
        for argv in [("kill", "bridge-smoke"), ("rm", "bridge-smoke"), ("server", "stop")]:
            try:
                p = hc(*argv, check=False)
                cleanup.append({"command": list(argv), "exit_code": p.returncode})
            except Exception as exc:
                cleanup.append({"command": list(argv), "error": str(exc)})
        result["cleanup"] = cleanup
        clean = all(c.get("exit_code") == 0 for c in cleanup)
        if received_signals:
            completed = False
            if failure is None:
                failure = f"received signal during cleanup: {received_signals}"
            result["signals"] = received_signals
        if completed and clean:
            try:
                shutil.rmtree(scratch)
                result.pop("scratch", None)
            except Exception as exc:
                clean = False
                failure = f"scratch cleanup failed: {exc}"
        if not clean and failure is None:
            failure = "hc cleanup failed; inspect the report before cleaning the isolated session"
        result["passed"] = completed and clean and failure is None
        result["phase"] = "finished"
        if failure is not None:
            result["error"] = failure
        try:
            write_report()
        finally:
            signal.signal(signal.SIGTERM, previous_term)
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "hc_version": result.get("hc_version"),
                "checks": checks,
                "report": str(args.output),
                "error": failure,
            }
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
