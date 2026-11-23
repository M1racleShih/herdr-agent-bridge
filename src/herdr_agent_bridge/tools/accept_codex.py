"""Run the read-only acceptance contract against real interactive Codex and hc.

Requires Python 3.11+, hc 0.1.18 and Codex CLI 0.166.0 (current evidence baseline),
existing working authentication/provider configuration. Does not approve dialogs.
Private machine evidence stays under .artifacts; use Ctrl-C for owned cleanup.
"""

from __future__ import annotations

import argparse
import ast
import json
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from herdr_agent_bridge.agent_bridge import Bridge, BridgeError, HcError, digest, save
from herdr_agent_bridge.runtime_paths import acceptance_directory
from herdr_agent_bridge.tools.acceptance_common import AuditRunner, finish_report, require

PROJECT = Path.cwd()


def final_result(bridge, task_id, deadline=360):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        task = bridge.observe(task_id, wait=20)
        print(
            json.dumps(
                {"task": task_id, "delivery": task["delivery"], "observed": task["observed"]},
                ensure_ascii=False,
            ),
            flush=True,
        )
        if task["observed"] in ("evidence_ready", "needs_human"):
            require(
                bool(task.get("evidence", {}).get("result")),
                "Real permission/trust UI requires human takeover; no approval was sent",
            )
            return task
        require(
            task["observed"] == "working",
            "Task cannot be resolved safely; inspect its private evidence, do not replay: "
            + task_id,
        )
    raise BridgeError("Acceptance deadline elapsed; task remains uncertain, no replay")


def test_names():
    tree = ast.parse((PROJECT / "tests/test_smoke_hc.py").read_text())
    return sorted(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )


def quote_checks(answer):
    sources = answer.get("sources", [])
    checks = []
    for source in sources:
        path, line, quote = source.get("path"), source.get("line"), source.get("quote")
        valid = path in ("src/herdr_agent_bridge/tools/smoke_hc.py", "tests/test_smoke_hc.py")
        if valid:
            lines = (PROJECT / path).read_text().splitlines()
            valid = (
                isinstance(line, int)
                and 0 < line <= len(lines)
                and isinstance(quote, str)
                and len(quote.strip()) >= 8
                and quote.strip() in lines[line - 1]
            )
        checks.append({"check": "source quotation " + str(path) + ":" + str(line), "passed": valid})
    unique = {(s.get("path"), s.get("line"), s.get("quote")) for s in sources}
    checks.append(
        {"check": "at least two distinct verifiable source quotations", "passed": len(unique) >= 2}
    )
    return checks


def control_checks(bridge, runner, root):
    """Actual hc processes for exit, unknown and failed startup; no models."""
    records = []
    for name, program, expected in [
        ("exit-check", [sys.executable, "-c", "raise SystemExit(7)"], "exited"),
        ("unknown-check", [sys.executable, "-c", "import signal; signal.pause()"], "unknown"),
        (
            "blocked-check",
            [
                sys.executable,
                "-c",
                'import signal; print("\\x1b]7501;state=blocked:app=acceptance-fixture\\x1b\\\\", flush=True); signal.pause()',
            ],
            "blocked",
        ),
    ]:
        receipt = bridge.hc(
            "run",
            "--bg",
            "--json",
            "--no-parent",
            "--no-notify",
            "--name",
            name,
            "--cwd",
            str(PROJECT),
            *program,
        )
        address = receipt["address"]
        try:
            waited = bridge.hc("wait", "--json", "--timeout", "2", address)
            terminal = bridge.hc("get", "--json", address)
            state = (terminal.get("status") or {}).get("state")
            if expected == "exited":
                passed = terminal["state"] == "finished" and terminal["exit_code"] == 7
            elif expected == "unknown":
                passed = (
                    terminal["state"] == "live" and state is None and waited["reason"] == "timeout"
                )
            else:
                require(state == "blocked", "fixture did not report blocked")
                try:
                    bridge.hc("send", "--json", address, "MUST_NOT_ARRIVE")
                    passed = False
                except HcError as exc:
                    error = json.loads(exc.call["stderr"])
                    passed = error.get("error", {}).get("kind") == "blocked"
            records.append(
                {
                    "check": expected,
                    "passed": passed,
                    "scope": "real hc + local program, not Codex permission UI",
                    "terminal": terminal,
                    "wait": waited,
                }
            )
        finally:
            bridge.hc("kill", address)
            bridge.hc("rm", address)
    invalid = "hab-command-does-not-exist-" + secrets.token_hex(8)
    try:
        bridge.hc(
            "run",
            "--bg",
            "--json",
            "--no-parent",
            "--no-notify",
            "--name",
            "startup-check",
            "--cwd",
            str(PROJECT),
            invalid,
        )
        records.append({"check": "startup_failure", "passed": False})
    except HcError as exc:
        error = json.loads(exc.call["stderr"])
        records.append(
            {
                "check": "startup_failure",
                "passed": error.get("error", {}).get("kind") == "spawn_failed",
                "scope": "real hc + nonexistent executable",
                "error": error,
            }
        )
    save(root / "control-checks.json", records)
    require(all(c["passed"] for c in records), "hc control checks failed")
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    if args.output is None:
        root = acceptance_directory(PROJECT, "codex", run_id)
    else:
        root = args.output.resolve()
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
    runner = AuditRunner(root / "calls.json")
    bridge = None
    report = {
        "format": 1,
        "passed": False,
        "phase": "running",
        "checks": [],
        "scope": "interactive Codex via hc; local coordinator only, no Grok Bot",
        "run_id": run_id,
        "not_verified": ["Grok Bot", "Herdr App", "real permission/trust dialog"],
        "failure_injection": "One actual send reply is discarded locally after hc returns; not a real network outage.",
    }
    save(root / "acceptance.json", report)

    def check(name, passed, **extra):
        item = {"check": name, "passed": bool(passed), **extra}
        report["checks"].append(item)
        save(root / "acceptance.json", report)
        require(passed, "Failed: " + name)

    previous_term = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt("received signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    try:
        files = [
            "README.md",
            "AGENTS.md",
            "docs/PROJECT.md",
            "src/herdr_agent_bridge/resources/bridge_protocol.json",
            "src/herdr_agent_bridge/tools/smoke_hc.py",
            "tests/test_smoke_hc.py",
        ]
        before = {path: digest((PROJECT / path).read_text()) for path in files}
        report["source_sha256"] = before
        report["implementation_sha256"] = {
            p: digest(
                (
                    Path(__file__).resolve().parents[1]
                    / Path(p).relative_to("src/herdr_agent_bridge")
                ).read_text()
            )
            for p in [
                "src/herdr_agent_bridge/agent_bridge.py",
                "src/herdr_agent_bridge/tools/accept_codex.py",
            ]
        }
        report["versions"] = {}
        for command in ("hc", "codex"):
            p = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=15)
            require(p.returncode == 0, command + " is unavailable")
            report["versions"][command] = p.stdout.strip()
        bridge = Bridge.create(root / "bridge", PROJECT, runner=runner)
        launch = bridge.launch()
        # hc can report an initial idle while the interactive app is still starting.
        from herdr_agent_bridge.grok_bridge import STARTUP_GUARD_MS

        startup = bridge.hc("get", "--json", launch["address"])
        bridge.identity(startup)
        guard = (startup["created_ms"] + STARTUP_GUARD_MS) / 1000 - time.time()
        if guard > 0:
            time.sleep(guard)
        terminal = bridge.observe(wait=30)
        require(
            terminal["state"] == "live"
            and (terminal.get("status") or {}).get("state") in ("idle", "done"),
            "Agent not ready; do not approve any startup dialog automatically",
        )
        report["mapping"] = {"session": bridge.data["session"], "address": launch["address"]}
        check("interactive_agent_started", True)
        first_id = "analysis-" + secrets.token_hex(8)
        first_prompt = (
            "Read src/herdr_agent_bridge/tools/smoke_hc.py and tests/test_smoke_hc.py. In answer return "
            "initial_task_ids (the two task identifiers in the smoke loop, in source order), "
            "blocked_error_kind (the exact error kind expected when blocked), "
            "regression_test_names (all three unittest test method names, sorted), "
            "success_requires_cleanup (boolean), "
            "limitation (one sentence explaining why these tests do not prove real-agent or Grok integration), "
            "fixture_proves_real_agent and fixture_proves_grok (booleans), "
            "and sources (at least two objects with path, line as a 1-based integer, quote as exact source text). "
            "Read the actual files, do not run the smoke tests. Keep the answer concise."
        )
        first = bridge.submit(first_id, first_prompt)
        # A real model is still working: a zero-second observer deadline must not replay it.
        timed = bridge.hc("wait", "--json", "--timeout", "0", launch["address"])
        check("wait_timeout_keeps_original_task", timed.get("reason") == "timeout", wait=timed)
        first = final_result(bridge, first_id)
        answer = first["evidence"]["result"]["answer"]
        checks = [
            {
                "check": "initial task ids match smoke source",
                "passed": answer.get("initial_task_ids") == ["task-001", "followup-001"],
            },
            {
                "check": "blocked error kind matches source",
                "passed": answer.get("blocked_error_kind") == "blocked",
            },
            {
                "check": "regression methods match Python AST",
                "passed": answer.get("regression_test_names") == test_names(),
            },
            {
                "check": "cleanup is part of success",
                "passed": answer.get("success_requires_cleanup") is True,
            },
            {
                "check": "explains fixture evidence limitation",
                "passed": isinstance(answer.get("limitation"), str)
                and len(answer["limitation"]) >= 20
                and answer.get("fixture_proves_real_agent") is False
                and answer.get("fixture_proves_grok") is False,
            },
            *quote_checks(answer),
        ]
        bridge.verify(first_id, checks)
        check(
            "first_result_independently_verified", all(c["passed"] for c in checks), details=checks
        )
        second_id = "followup-" + secrets.token_hex(8)
        runner.drop_next_send_reply = True
        second = bridge.submit(
            second_id,
            "Follow up on your previous answer. For the regression test that rejects evidence from "
            "the wrong task, return method_name, asserted_message (the exact message fragment checked), "
            "and sources (two exact source quotations with path, line, quote). "
            "Also return previous_task_id, using the task identifier of our previous request from memory.",
            parent=first_id,
        )
        check("lost_reply_records_unknown_before_reconciliation", second["delivery"] == "unknown")
        # Both exact replay and creating a replacement task must be refused locally.
        for task_id, parent in [
            (second_id, first_id),
            ("replacement-" + secrets.token_hex(6), second_id),
        ]:
            try:
                bridge.submit(task_id, "Must not be sent", parent=parent)
                prevented = False
            except BridgeError:
                prevented = True
            check("refuses_replay_" + task_id.split("-")[0], prevented)
        second = final_result(bridge, second_id)
        answer2 = second["evidence"]["result"]["answer"]
        checks2 = [
            {
                "check": "same-task follow-up answer matches source",
                "passed": answer2.get("method_name")
                == "test_optimized_run_rejects_wrong_task_evidence"
                and answer2.get("asserted_message") == "missing task evidence",
            },
            {
                "check": "previous request remembered",
                "passed": answer2.get("previous_task_id") == first_id,
            },
            {
                "check": "same native Codex thread, distinct native turn",
                "passed": second["evidence"]["thread_id"] == first["evidence"]["thread_id"]
                and second["evidence"]["agent_turn_id"] != first["evidence"]["agent_turn_id"],
            },
            *quote_checks(answer2),
        ]
        bridge.verify(second_id, checks2)
        check(
            "followup_and_delivery_reconciliation_verified",
            all(c["passed"] for c in checks2),
            details=checks2,
        )
        third_id = "question-" + secrets.token_hex(8)
        bridge.submit(
            third_id,
            "Before doing further work, ask me which language to use for the final explanation. "
            "Do not choose a language yourself. Return kind=question and answer.question.",
            parent=second_id,
        )
        third = final_result(bridge, third_id)
        check(
            "real_agent_returns_question_for_human",
            third["observed"] == "needs_human"
            and third.get("evidence", {}).get("agent_completed") is True
            and third.get("evidence", {}).get("result", {}).get("kind") == "question"
            and bool(third.get("evidence", {}).get("result", {}).get("answer", {}).get("question")),
            question=third.get("evidence", {}).get("result", {}).get("answer"),
        )
        fourth_id = "answer-" + secrets.token_hex(8)
        bridge.submit(
            fourth_id,
            'Use English. Return kind=result and answer.language="English", '
            "answer.previous_question_task_id identifying your preceding question request from memory, "
            "answer.done_verifies_success (boolean), and answer.explanation: one sentence about why a done terminal state does not verify a task result.",
            parent=third_id,
        )
        fourth = final_result(bridge, fourth_id)
        answer4 = fourth["evidence"]["result"]["answer"]
        checks4 = [
            {
                "check": "human clarification continues same thread",
                "passed": answer4.get("language") == "English"
                and answer4.get("previous_question_task_id") == third_id
                and fourth["evidence"]["thread_id"] == first["evidence"]["thread_id"]
                and answer4.get("done_verifies_success") is False
                and bool(answer4.get("explanation")),
            }
        ]
        bridge.verify(fourth_id, checks4)
        check("human_answer_continues_task", all(c["passed"] for c in checks4), details=checks4)
        sends = [c for c in runner.calls if c["args"][0] == "send"]
        check("four_tasks_exactly_four_send_attempts", len(sends) == 4)
        controls = control_checks(bridge, runner, root)
        for control in controls:
            check("control_" + control["check"], control["passed"], scope=control["scope"])
        check(
            "six_task_source_files_unchanged",
            all(digest((PROJECT / path).read_text()) == h for path, h in before.items()),
            paths=files,
        )
        report["results"] = [
            {
                "task_id": t["id"],
                "parent": t["parent"],
                "delivery": t["delivery"],
                "observed": t["observed"],
                "verified_success": t["verified_success"],
                "thread_id": t.get("evidence", {}).get("thread_id"),
                "agent_turn_id": t.get("evidence", {}).get("agent_turn_id"),
                "result": t.get("evidence", {}).get("result"),
            }
            for t in bridge.data["tasks"]
        ]
        report["passed"] = True
    except (Exception, KeyboardInterrupt) as exc:
        report["passed"] = False
        report["error"] = type(exc).__name__ + ": " + str(exc)
        print(report["error"], file=sys.stderr, flush=True)
    finally:
        try:
            if bridge is not None:
                sessions = Path(bridge.data["scratch"]) / "codex" / "sessions"
                report["native_capture"] = []
                if sessions.is_dir() and not sessions.is_symlink():
                    for index, path in enumerate(sorted(sessions.rglob("*.jsonl"))):
                        require(
                            not path.is_symlink() and path.resolve().is_relative_to(sessions),
                            "isolated session file changed during capture",
                        )
                        target = root / f"codex-session-{index}.jsonl"
                        shutil.copyfile(path, target)
                        target.chmod(0o600)
                        report["native_capture"].append(target.name)
        except (Exception, KeyboardInterrupt) as exc:
            report["passed"] = False
            report["capture_error_type"] = type(exc).__name__
        finish_report(bridge, report, root / "acceptance.json")
        signal.signal(signal.SIGTERM, previous_term)
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "report": str(root / "acceptance.json"),
                "error": report.get("error"),
                "cleanup_error": report.get("cleanup_error"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
