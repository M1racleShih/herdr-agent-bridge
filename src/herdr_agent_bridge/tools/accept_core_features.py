"""Real hc/Pi control and event acceptance, using dedicated cores and saved evidence."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from herdr_agent_bridge.agent_bridge import run_hc, save
from herdr_agent_bridge.bridge_common import PYTHON_NO_USER_SITE, PYTHON_USER_BASE, digest
from herdr_agent_bridge.core_events import CoreEvents
from herdr_agent_bridge.development import snapshot
from herdr_agent_bridge.grok_bridge import Manager
from herdr_agent_bridge.runtime_paths import RUNS, acceptance_directory
from herdr_agent_bridge.tools.acceptance_common import AuditRunner, require

PROJECT = Path.cwd()
OWNER = {"bot_id": "core-feature-acceptance", "conversation_id": "local-coordinator"}


def observe_until(manager, predicate, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.observe(wait=10)
        if predicate(result):
            return result
        require(
            result["execution"]["state"] == "live"
            and result["execution"]["status"] not in ("blocked", "error"),
            "Agent needs inspection; original task retained",
        )
    raise RuntimeError("Acceptance observation expired; inspect the original run")


def interrupt(manager, check):
    ready = observe_until(manager, lambda result: result["input_ready"])
    check("pi_ready", ready["input_ready"])
    manager.submit(
        "interrupt-first",
        "Read src/herdr_agent_bridge/pi_evidence.py and tests/test_pi_evidence.py. "
        "Then explain every parsing rule and test case in detail with source references. "
        "This round will be interrupted as part of an acceptance test. Do not change files.",
    )
    working = manager.observe()
    check("real_agent_working", working["execution"]["status"] == "working")
    stopped = manager.interrupt(
        "interrupt-first", actor="acceptance-coordinator", note="Test change of direction"
    )
    if stopped["task"]["observed"] != "interrupted":
        stopped = observe_until(manager, lambda result: result["task"]["observed"] == "interrupted")
    check(
        "native_abort_correlated",
        stopped["task"]["observed"] == "interrupted"
        and stopped["task"]["delivery"] == "received"
        and not stopped["task"]["verified_success"],
    )
    repeated = manager.interrupt(
        "interrupt-first", actor="acceptance-coordinator", note="Test change of direction"
    )
    check(
        "repeat_keeps_original_stop", repeated["task"]["interrupt"] == stopped["task"]["interrupt"]
    )
    manager.submit(
        "interrupt-followup",
        "The previous round was deliberately interrupted. Read the first line of README.md. "
        'Return answer with exactly these fields: marker="F5_INTERRUPT_CONTINUED", '
        "title=the exact first line of README.md. Do not change files.",
        parent="interrupt-first",
    )
    done = observe_until(manager, lambda result: result["task"]["observed"] == "evidence_ready")
    answer = done["task"]["evidence"]["result"]["answer"]
    check(
        "followup_answer_independently_checked",
        answer.get("marker") == "F5_INTERRUPT_CONTINUED"
        and answer.get("title") == (PROJECT / "README.md").read_text().splitlines()[0],
    )
    check(
        "same_native_session",
        done["task"]["evidence"]["thread_id"] == stopped["task"]["evidence"]["thread_id"],
    )
    check("same_terminal", done["execution"]["address"] == stopped["execution"]["address"])
    return done


def keep(manager, check):
    observe_until(manager, lambda result: result["input_ready"])
    marker = "F5_RECOVERED_" + secrets.token_hex(8)
    manager.submit(
        "keep-first",
        "Remember this context token: " + marker + ". "
        "Read src/herdr_agent_bridge/pi_evidence.py and tests/test_pi_evidence.py and explain every test in detail. "
        "This round will be stopped by a process-loss acceptance test. Do not change files.",
    )
    bridge = manager.bridge()
    terminal = bridge.hc("get", "--json", bridge.target())
    bridge.identity(terminal)
    check(
        "kept_pi_working",
        terminal.get("keep") is True and (terminal.get("status") or {}).get("state") == "working",
    )
    before = snapshot(PROJECT)
    stream = subprocess.Popen(
        ["hc", "events", "--json", "--terminal", terminal["address"]],
        env=bridge.env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    try:
        selector = selectors.DefaultSelector()
        selector.register(stream.stdout, selectors.EVENT_READ)
        # Only this run's exact, identity-checked Pi process receives the signal.
        os.kill(terminal["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 60
        restarted = False
        buffer = b""
        while time.monotonic() < deadline and not restarted:
            for key, _ in selector.select(timeout=min(10, deadline - time.monotonic())):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    raise RuntimeError("Recovery event stream closed")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line)
                    event = message.get("params", message)
                    restarted = restarted or event.get("type") == "restarted"
        selector.close()
        check("real_core_restarted_shell", restarted)
    finally:
        stream.terminate()
        stream.wait(timeout=10)
        stream.stdout.close()
    manager.observe()
    review = manager.recovery_control("recovery-status")["control_result"]
    check(
        "loss_closes_task_input",
        review["state"] == "needed" and not manager.status()["input_ready"],
    )
    restored = manager.recovery_control(
        "recover",
        fingerprint=review["fingerprint"],
        actor="acceptance-coordinator",
        review="Reviewed current source and native session",
    )
    if (restored.get("recovery") or {}).get("state") != "restored":
        restored = observe_until(
            manager, lambda result: (result.get("recovery") or {}).get("state") == "restored"
        )
    check(
        "original_session_restored",
        restored["input_ready"] and restored["task"]["observed"] == "interrupted",
    )
    original_thread = restored["task"]["evidence"]["thread_id"]
    manager.submit(
        "keep-followup",
        "Recover the exact context token from the previous user turn, starting with F5_RECOVERED_. "
        "Return answer with marker equal to that token. Do not change files.",
        parent="keep-first",
    )
    done = observe_until(manager, lambda result: result["task"]["observed"] == "evidence_ready")
    check(
        "saved_context_independently_checked",
        done["task"]["evidence"]["result"]["answer"].get("marker") == marker,
    )
    check("same_native_session", done["task"]["evidence"]["thread_id"] == original_thread)
    check("same_kept_terminal", done["execution"]["address"] == terminal["address"])
    check("source_unchanged", snapshot(PROJECT) == before)
    return done


def receiver(root):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                self.send_error(413)
                return
            payload = json.loads(self.rfile.read(length))
            receipts.append(
                {
                    "event_id": payload["event_id"],
                    "kind": payload["kind"],
                    "task_id": payload["task_id"],
                }
            )
            save(root / "receipts.json", receipts)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"accepted")

        def log_message(self, *_):
            pass

    receipts = []
    server = HTTPServer(("127.0.0.1", 0), Handler)
    save(root / "receiver.json", {"port": server.server_port})
    save(root / "receipts.json", receipts)
    print("\x1b]7501;state=idle:app=feature-receiver\x1b\\", flush=True)
    server.serve_forever()


def events(manager, check):
    observe_until(manager, lambda result: result["input_ready"])
    bridge = manager.bridge()
    evidence_root = manager.root / "event-acceptance"
    evidence_root.mkdir(mode=0o700)
    token = secrets.token_hex(10)
    received = bridge.hc(
        "run",
        "--bg",
        "--json",
        "--no-parent",
        "--no-notify",
        "--name",
        "events-receiver-" + token[:8],
        "--label",
        "bridge_receiver=" + token,
        "env",
        "-u",
        "PYTHONPATH",
        "-u",
        "PYTHONHOME",
        "-u",
        "PYTHONUSERBASE",
        "PYTHONUSERBASE=" + PYTHON_USER_BASE,
        "PYTHONNOUSERSITE=" + PYTHON_NO_USER_SITE,
        sys.executable,
        "-P",
        "-u",
        "-m",
        "herdr_agent_bridge.tools.accept_core_features",
        "receiver",
        "--receiver-root",
        str(evidence_root),
    )
    address = received["address"]
    try:
        bridge.hc("wait", "--json", "--timeout", "10", address)
        port = json.loads((evidence_root / "receiver.json").read_text())["port"]
        config = evidence_root / "webhook.json"
        save(
            config,
            {**OWNER, "url": "https://acceptance.invalid/routine", "key": secrets.token_hex(32)},
        )
        manager.configure(config)

        def sender(_url, _key, body):
            with urlopen(
                Request(
                    "http://127.0.0.1:" + str(port),
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                ),
                timeout=10,
            ) as response:
                return response.status

        manager.sender = sender
        manager.submit(
            "events-first",
            "Read src/herdr_agent_bridge/pi_evidence.py and tests/test_pi_evidence.py. "
            "Review their parsing and regression contracts carefully, then return answer "
            'with marker="F5_EVENTS_COMPLETE". Do not change files.',
        )
        sources = []

        class FaultedEvents(CoreEvents):
            def __init__(self, terminal, env):
                super().__init__(terminal, env)
                self.disconnected = False
                self.labelled = False
                original = self.cursor.accept

                def replay(message):
                    accepted = original(message)
                    if accepted and message.get("method") == "event":
                        require(original(message) is False, "replayed real event must be ignored")
                    return accepted

                self.cursor.accept = replay
                sources.append(self)

            def read(self, timeout):
                changed = super().read(timeout)
                if self.stats["snapshots"] >= 1 and not self.disconnected:
                    self.disconnected = True
                    self.process.terminate()  # This exact hc subscriber handle, leaving Pi running.
                    self.process.wait(timeout=5)
                elif self.stats["snapshots"] >= 2 and not self.labelled:
                    self.labelled = True
                    bridge.hc("label", bridge.target(), "f5_replay=" + token)
                return changed

        result = manager.watch(180, event_source=FaultedEvents)
        check("actual_event_stream_used", bool(sources) and sources[0].stats["events"] > 0)
        check(
            "actual_disconnect_and_reconnect",
            sources[0].stats["connections"] >= 2 and sources[0].stats["snapshots"] >= 2,
        )
        check("replayed_real_events_ignored", sources[0].stats["duplicates"] > 0)
        check(
            "native_result_independently_checked",
            result["task"]["observed"] == "evidence_ready"
            and result["task"]["evidence"]["result"]["answer"].get("marker")
            == "F5_EVENTS_COMPLETE",
        )
        duplicate = manager.watch(10)
        check("same_result_same_event_id", result["event_id"] == duplicate["event_id"])
        receipt_list = json.loads((evidence_root / "receipts.json").read_text())
        check(
            "one_real_http_delivery",
            len(receipt_list) == 1 and receipt_list[0]["event_id"] == result["event_id"],
        )
        check("one_outbox_attempt", len(manager.outbox().get(result["event_id"])["attempts"]) == 1)
        check("subscriber_cleaned", sources[0].process is None)
        check(
            "no_business_input_replayed",
            sum(call["args"][0] == "send" for call in manager.runner.calls) == 1,
        )
        # Exercise the normal managed_watch bootstrap too. This second local
        # round deliberately has no notification endpoint; its event stays in outbox.
        with manager.locked():
            manager.data["webhook_config"] = None
            save(manager.path, manager.data)
        manager.submit(
            "events-managed",
            "Read docs/PROJECT.md and summarize its task controls. "
            'Return answer with marker="F5_MANAGED_EVENTS". Do not change files.',
            parent="events-first",
        )
        monitor = manager.monitor(180)["monitor"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            waited = bridge.hc("wait", "--json", "--timeout", "20", monitor["address"], timeout=30)
            if waited.get("ended"):
                break
        managed = manager.status()
        subscription = (managed.get("monitor") or {}).get("subscription") or {}
        check(
            "managed_bootstrap_uses_real_events",
            subscription.get("snapshots", 0) >= 1
            and subscription.get("events", 0) >= 1
            and subscription.get("state") == "closed",
        )
        check(
            "managed_result_independently_checked",
            managed["task"]["observed"] == "evidence_ready"
            and managed["task"]["evidence"]["result"]["answer"].get("marker")
            == "F5_MANAGED_EVENTS",
        )
        check(
            "exactly_two_business_inputs",
            sum(call["args"][0] == "send" for call in manager.runner.calls) == 2,
        )
        return {
            **result,
            "subscription": dict(sources[0].stats),
            "managed_subscription": subscription,
            "notification_transport": "real loopback HTTP via injected sender; local receiver, no Grok",
        }
    finally:
        terminal = bridge.hc("get", "--json", address)
        require(
            terminal.get("labels", {}).get("bridge_receiver") == token, "receiver identity changed"
        )
        bridge.hc("kill", address)
        bridge.hc("rm", address)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feature", choices=("interrupt", "keep", "events", "receiver"))
    parser.add_argument("--receiver-root", type=Path)
    parser.add_argument("--model", default="openai/gpt-6.1-sol")
    parser.add_argument("--thinking", default="high")
    args = parser.parse_args(argv)
    if args.feature == "receiver":
        require(
            args.receiver_root is not None and args.receiver_root.is_dir(),
            "receiver directory required",
        )
        receiver(args.receiver_root)
        return 0
    name = args.feature + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    root = acceptance_directory(PROJECT, "core-features", name)
    scratch = Path(tempfile.mkdtemp(prefix="hab-core-acceptance-"))
    for key, part in [
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_RUNTIME_DIR", "run"),
        ("XDG_CACHE_HOME", "cache"),
    ]:
        (scratch / part).mkdir(mode=0o700)
        os.environ[key] = str(scratch / part)
    for key in list(os.environ):
        if key.startswith("HC_"):
            os.environ.pop(key)
    os.environ["HC_SESSION"] = "core-acceptance-" + secrets.token_hex(6)
    (scratch / "config" / "herdr-core").mkdir()
    (scratch / "config" / "herdr-core" / "config.toml").write_text("notes = false\n")
    report = {
        "feature": args.feature,
        "passed": False,
        "phase": "running",
        "checks": [],
        "scope": "real interactive Pi and hc, local Manager caller",
        "recovery": {"scratch": str(scratch), "session": os.environ["HC_SESSION"]},
        "versions": {
            program: subprocess.run(
                [program, "--version"], capture_output=True, text=True, timeout=10
            ).stdout.strip()
            for program in ("hc", "pi")
        },
    }
    report["source_sha256"] = {
        str(path.relative_to(PROJECT)): digest(path.read_text())
        for pattern in ("*.py", "*.mjs", "*.json")
        for path in sorted((PROJECT / "src/herdr_agent_bridge").rglob(pattern))
    }
    report_path = root / "acceptance.json"
    save(report_path, report)
    print(str(report_path.relative_to(PROJECT)), flush=True)
    manager = None
    runner = AuditRunner(root / "calls.json")

    def check(name, passed):
        report["checks"].append({"check": name, "passed": bool(passed)})
        save(report_path, report)
        print(json.dumps(report["checks"][-1]), flush=True)
        require(passed, name)

    try:
        run = PROJECT / RUNS / ("core-feature-" + name)
        report["run"] = str(run.relative_to(PROJECT))
        save(report_path, report)
        manager = Manager.create(
            run,
            PROJECT,
            OWNER,
            model=args.model,
            thinking=args.thinking,
            agent="pi",
            runtime="native",
            runner=runner,
        )
        report["result"] = {"interrupt": interrupt, "keep": keep, "events": events}[args.feature](
            manager, check
        )
        check(
            "one_agent_launch",
            sum(
                call["args"][0] == "run"
                and any(arg.startswith("bridge_run=") for arg in call["args"])
                for call in runner.calls
            )
            == 1,
        )
        if args.feature == "interrupt":
            check(
                "one_stop_attempt",
                sum(call["args"][0] == "interrupt" for call in runner.calls) == 1,
            )
        elif args.feature == "keep":
            check(
                "one_resume_and_two_business_inputs",
                sum(call["args"][0] == "send" for call in runner.calls) == 3,
            )
        report["phase"] = "accepted"
    except BaseException as exc:
        report["phase"] = "failed"
        report["error_type"] = type(exc).__name__
        report["private_error"] = str(exc)
        print(json.dumps({"failed": type(exc).__name__}), flush=True)
    finally:
        if manager is not None:
            closed = manager.close()
            check("owned_run_cleaned", closed["closed"])
        listing = run_hc(["ls", "--all", "--json"], os.environ, 10)
        if not listing.get("terminals"):
            run_hc(["server", "stop"], os.environ, 10)
            shutil.rmtree(scratch)
            report["core_cleaned"] = True
        report["passed"] = report["phase"] == "accepted" and report.get("core_cleaned") is True
        save(report_path, report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
