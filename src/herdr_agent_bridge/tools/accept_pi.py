"""Real read-only Pi/hc acceptance through the local Grok manager CLI layer.

Calls the configured model. It does not invoke Grok, import Pi extensions, or
approve trust/permission dialogs. All sessions and evidence are dedicated.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import signal
import time
from pathlib import Path

from herdr_agent_bridge.agent_bridge import BridgeError, digest, save
from herdr_agent_bridge.grok_bridge import Manager
from herdr_agent_bridge.readiness import probe
from herdr_agent_bridge.readonly_contracts import PROMPTS
from herdr_agent_bridge.runtime_paths import RUNS, acceptance_directory, new_manager_run
from herdr_agent_bridge.tools.acceptance_common import AuditRunner, finish_report, require

PROJECT = Path.cwd()
OWNER = {"bot_id": "local-pi-acceptance", "conversation_id": "local-coordinator"}


class Cleanup:
    def __init__(self, root):
        self.root = root

    def close(self):
        manager = Manager(self.root, OWNER)
        journal = self.root / "bridge" / "journal.json"
        scratch = Path(manager.bridge().data["scratch"]) if journal.is_file() else None
        result = manager.close()
        checks = [*result["cleanup"], {"operation": "manager_closed", "passed": result["closed"]}]
        if scratch is not None:
            checks.append(
                {
                    "operation": "private_agent_home_removed",
                    "passed": result["closed"] and not scratch.exists(),
                }
            )
        return checks


def ready(manager, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.observe(wait=10)
        if result["input_ready"]:
            return
        state = result["execution"]["status"]
        require(
            state not in ("blocked", "error") and result["execution"]["state"] == "live",
            "Pi startup needs inspection or human takeover; no input attempted",
        )
    raise BridgeError("Pi startup observation expired; no input attempted")


def completed(manager, task_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.observe(task_id, wait=10)
        task = result["task"]
        print(
            json.dumps(
                {"task_id": task_id, "delivery": task["delivery"], "observed": task["observed"]}
            ),
            flush=True,
        )
        if task["observed"] in ("evidence_ready", "needs_human"):
            require(
                bool(task["evidence"]["result"]) and task["evidence"]["agent_completed"],
                "Human takeover required; no permission/trust answer sent",
            )
            return task
        require(
            task["observed"] not in ("agent_error", "exited"),
            "Pi failed or exited; inspect original task, never replay",
        )
    raise BridgeError("Task observation expired; original delivery remains unresolved, no replay")


def source_reads(entries, user_entry_id, project):
    """Return named sources successfully read within the analysis user turn."""
    sources = {"src/herdr_agent_bridge/tools/smoke_hc.py", "tests/test_smoke_hc.py"}
    expected = {str((Path(project) / name).resolve()): name for name in sources}
    active, calls, read = False, {}, set()
    for entry in entries:
        message = entry.get("message", {})
        if entry.get("id") == user_entry_id and message.get("role") == "user":
            active = True
            continue
        if not active:
            continue
        if message.get("role") == "user":
            break
        if message.get("role") == "assistant":
            for block in message.get("content", []):
                if block.get("type") != "toolCall" or block.get("name") != "read":
                    continue
                path = block.get("arguments", {}).get("path")
                if not isinstance(path, str):
                    continue
                resolved = Path(path) if Path(path).is_absolute() else Path(project) / path
                name = expected.get(str(resolved.resolve()))
                if name and isinstance(block.get("id"), str):
                    calls[block["id"]] = name
        elif (
            message.get("role") == "toolResult"
            and message.get("toolName") == "read"
            and message.get("isError") is False
            and message.get("toolCallId") in calls
        ):
            read.add(calls[message["toolCallId"]])
    return read


def capture_native_sessions(root, evidence=None):
    """Snapshot this run's native evidence before deleting copied credentials."""
    evidence = root if evidence is None else evidence
    journal = root / "bridge" / "journal.json"
    if not journal.is_file():
        return []
    data = json.loads(journal.read_text())
    session_dir = Path(data["scratch"]) / "pi" / "sessions"
    if session_dir.is_symlink():
        raise BridgeError("native session directory changed; refusing evidence capture")
    paths = list(session_dir.rglob("*.jsonl"))
    captures = []
    for index, path in enumerate(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(session_dir.resolve()):
            raise BridgeError("native session path changed; refusing evidence capture")
        name = "pi-session.jsonl" if len(paths) == 1 else "pi-session-" + str(index) + ".jsonl"
        target = evidence / name
        shutil.copyfile(path, target)
        target.chmod(0o600)
        captures.append(name)
    return captures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--model", help="Pi provider/model; default: saved Pi selection")
    parser.add_argument(
        "--timeout", type=int, default=180, help="observation bound per stage, seconds"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600 seconds")
    name = "pi-local-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    try:
        root = new_manager_run(args.run or PROJECT / RUNS / name, PROJECT)
    except BridgeError as exc:
        parser.error(str(exc))
    if root.exists():
        parser.error("--run must be a new dedicated directory below .artifacts/runs")
    evidence = acceptance_directory(PROJECT, "pi", name)
    output = evidence / "acceptance.json"
    report = {
        "format": 1,
        "phase": "running",
        "passed": False,
        "agent": "pi",
        "run": str(root.relative_to(PROJECT)),
        "checks": [],
        "scope": "real interactive Pi and hc via local manager; no Grok Bot",
        "not_verified": [
            "Grok Bot to Pi",
            "real network outage",
            "real permission/trust takeover",
            "OS sandbox or restriction of read access to project",
        ],
        "failure_injection": "Discard one actual hc send reply locally; not a real network outage.",
        "clarification": "Script supplies the deterministic English acceptance answer.",
    }
    save(output, report)
    runner = AuditRunner(evidence / "calls.json")
    manager = None

    def check(name, passed, **extra):
        report["checks"].append({"check": name, "passed": bool(passed), **extra})
        save(output, report)
        require(passed, "Failed: " + name)

    previous_term = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt("acceptance interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        report["probe"] = probe(PROJECT, agent="pi")
        check("isolated_prerequisites", report["probe"]["ready"])
        files = ("src/herdr_agent_bridge/tools/smoke_hc.py", "tests/test_smoke_hc.py")
        before = {path: digest((PROJECT / path).read_text()) for path in files}
        report["source_sha256"] = before
        manager = Manager.create(root, PROJECT, OWNER, model=args.model, runner=runner, agent="pi")
        initial_bridge = manager.bridge()
        report["model"] = initial_bridge.data["model"]
        report["auth_mode"] = initial_bridge.data.get("pi_auth_mode")
        if report["auth_mode"] == "native_shared":
            check(
                "oauth_tokens_not_copied",
                not (Path(initial_bridge.data["scratch"]) / "pi" / "auth.json").exists(),
            )
        ready(manager, args.timeout)
        check("interactive_pi_input_ready", True)
        results, parent = [], None
        for contract in ("analysis", "followup", "question", "clarification"):
            task_id = contract + "-pi"
            if contract == "followup":
                runner.drop_next_send_reply = True
            submitted = manager.submit(task_id, PROMPTS[contract], parent)["task"]
            if contract == "followup":
                check("lost_send_reply_is_unknown", submitted["delivery"] == "unknown")
                for replacement in (task_id, "replacement-pi"):
                    try:
                        manager.submit(replacement, "Must never be sent", task_id)
                        prevented = False
                    except BridgeError:
                        prevented = True
                    check("refuses_replay_" + replacement, prevented)
            completed(manager, task_id, args.timeout)
            verified = manager.verify(task_id, contract)
            check(
                contract + "_independent_contract", verified["passed"], details=verified["checks"]
            )
            task = verified["task"]
            check(
                contract + "_success_boundary", task["verified_success"] is (contract != "question")
            )
            results.append(task)
            parent = task_id
        report["results"] = results
        check("same_native_session", len({t["evidence"]["thread_id"] for t in results}) == 1)
        check(
            "four_distinct_native_user_entries",
            len({t["evidence"]["agent_turn_id"] for t in results}) == 4,
        )
        check("exactly_four_send_attempts", sum(c["args"][0] == "send" for c in runner.calls) == 4)
        bridge = manager.bridge()
        sessions = list((Path(bridge.data["scratch"]) / "pi" / "sessions").rglob("*.jsonl"))
        check("one_private_native_session", len(sessions) == 1)
        entries = [json.loads(line) for line in sessions[0].read_text().splitlines()]
        messages = [entry["message"] for entry in entries if entry.get("type") == "message"]
        tools = {
            tool["name"]
            for msg in messages
            if msg.get("role") == "system"
            for tool in msg.get("toolsAdded", [])
        }
        check("only_readonly_tools_declared", tools == {"read", "grep", "find", "ls"})
        read = source_reads(entries, results[0]["evidence"]["agent_turn_id"], PROJECT)
        check("real_source_reads_in_analysis_turn", read == set(files), sources=sorted(read))
        check(
            "task_source_files_unchanged",
            all(digest((PROJECT / path).read_text()) == h for path, h in before.items()),
        )
        report["mapping"] = {"session": bridge.data["session"], "address": bridge.data["address"]}
        report["passed"] = True
    except (Exception, KeyboardInterrupt) as exc:
        report["passed"] = False
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)  # Private artifact only, never printed as native output.
    finally:
        try:
            report["native_capture"] = {
                "files": capture_native_sessions(root, evidence),
                "snapshot_only": True,
            }
        except (Exception, KeyboardInterrupt) as exc:
            report["passed"] = False
            report["capture_error_type"] = type(exc).__name__
        cleanup = Cleanup(root) if (root / "manager.json").is_file() else None
        finish_report(cleanup, report, output)
        signal.signal(signal.SIGTERM, previous_term)
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "report": str(output.relative_to(PROJECT)),
                "error_type": report.get("error_type"),
                "cleanup_passed": bool(report.get("cleanup"))
                and all(c["passed"] for c in report["cleanup"]),
            }
        ),
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
