"""Recognize one corroborated two-pack continuation in native Pi history.

This is transcript correlation, not sender authentication or task verification.
The commands and their outputs are inspected as text; nothing in them is run.
"""

from __future__ import annotations

import json
import re
import shlex

_PACK = r"[0-9a-f]{32}"
_ADDRESS = r"[A-Za-z0-9_.:-]+"
_NOTICE = re.compile(
    rf'<hc from="(?P<sender>{_ADDRESS})(?: {_ADDRESS})?">'
    rf"Herdr two-pack receipt saved for (?P<pack>{_PACK})/"
    r"(?P<role>coder|cleaner)/(?P<round>[1-9][0-9]*) "
    r"\((?P<status>done|pass)\)\. Decision: (?P<decision>active|completed)\. "
    r"Inspect with [^<>\r\n]+\.</hc>"
)
_BINDING_FILTER = "{id,phase,leader,workers,current,summary,round_count:(.rounds|length)}"
_COMPLETED_FILTER = (
    "{id,phase,leader,workers,branch,baseline,current,summary,"
    "assignments:[.assignments[]|{id,role,target,rejections,receipt}]}"
)
_TERMINALS_FILTER = ".terminals[]|{address,status,turn}"


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _constant(value):
    raise ValueError("invalid constant")


def _command(arguments):
    """Accept only the observed read-only resume/projection command grammar."""
    if not isinstance(arguments, dict) or set(arguments) != {"command", "timeout"}:
        return None
    command, timeout = arguments["command"], arguments["timeout"]
    if (
        not isinstance(command, str)
        or len(command) > 4096
        or any(c in command for c in "$`\\\n\r")
        or type(timeout) not in (int, float)
        or not 0 < timeout <= 300
    ):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if tokens[:4] == ["set", "-o", "pipefail", ";"]:
        tokens = tokens[4:]
    if len(tokens) != 24:
        return None
    if (
        tokens[:2] != ["hc", "wait"]
        or re.fullmatch(_ADDRESS, tokens[2]) is None
        or tokens[3] != "--timeout"
        or re.fullmatch(r"[1-9][0-9]{0,2}", tokens[4]) is None
        or tokens[5:8] != ["--json", ";", "python3"]
        or any(re.fullmatch(r"/[A-Za-z0-9_./-]+", p) is None for p in tokens[8:10])
        or any(part == ".." for p in tokens[8:10] for part in p.split("/"))
        # Recognize the transcript's two-pack CLI invocation without opening it.
        or not tokens[8].endswith("/scripts/handoff.py")
        or tokens[10] != "two"
        or re.fullmatch(_PACK, tokens[11]) is None
        or tokens[12:15] != ["resume", "|", "jq"]
        or tokens[16:23] != [";", "hc", "ls", "--json", "|", "jq", "-c"]
        or "".join(tokens[23].split()) != _TERMINALS_FILTER
    ):
        return None
    projection = "".join(tokens[15].split())
    if projection == _BINDING_FILTER:
        kind = "binding"
    elif projection == _COMPLETED_FILTER:
        kind = "completed"
    else:
        return None
    return kind, tokens[11], tokens[2], tokens[8], tokens[9]


def _objects(message):
    content = message.get("content")
    if (
        message.get("isError") is not False
        or not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or content[0].get("type") != "text"
        or not isinstance(content[0].get("text"), str)
        or len(content[0]["text"]) > 262144
    ):
        return None
    text = content[0]["text"]
    decoder = json.JSONDecoder(object_pairs_hook=_unique, parse_constant=_constant)
    values = []
    try:
        while text.strip():
            text = text.lstrip()
            value, end = decoder.raw_decode(text)
            values.append(value)
            if len(values) > 5:
                return None
            text = text[end:]
    except (ValueError, RecursionError):
        return None
    return values if len(values) == 5 and all(isinstance(v, dict) for v in values) else None


def _snapshot(message, command):
    objects = _objects(message)
    if objects is None:
        return None
    kind, pack, waited, _, _ = command
    wait, snapshot, *terminals = objects
    if (
        not {"children", "ended", "exit_code", "reason", "signal", "status", "terminal", "turn"}
        <= wait.keys()
        or set(wait)
        - {
            "children",
            "ended",
            "exit_code",
            "reason",
            "signal",
            "status",
            "terminal",
            "turn",
            "from",
        }
        or wait["terminal"] != waited
        or type(wait["turn"]) is not int
        or not isinstance(wait["children"], list)
        or type(wait["ended"]) is not bool
        or snapshot.get("id") != pack
        or not isinstance(snapshot.get("workers"), dict)
        or set(snapshot["workers"]) != {"coder", "cleaner"}
        or any(
            not isinstance(x, str) or re.fullmatch(_ADDRESS, x) is None
            for x in (snapshot.get("leader"), *snapshot["workers"].values())
        )
    ):
        return None
    addresses = {snapshot["leader"], *snapshot["workers"].values()}
    if (
        len(addresses) != 3
        or any(
            set(t) != {"address", "status", "turn"}
            or not isinstance(t["address"], str)
            or not isinstance(t["status"], str)
            or type(t["turn"]) is not int
            for t in terminals
        )
        or {t["address"] for t in terminals} != addresses
        or waited != snapshot["workers"]["coder" if kind == "binding" else "cleaner"]
    ):
        return None
    if kind == "binding":
        if (
            set(snapshot)
            != {"id", "phase", "leader", "workers", "current", "summary", "round_count"}
            or snapshot["phase"] != "active"
            or type(snapshot["round_count"]) is not int
            or snapshot["round_count"] < 1
        ):
            return None
    elif (
        set(snapshot)
        != {
            "id",
            "phase",
            "leader",
            "workers",
            "branch",
            "baseline",
            "current",
            "summary",
            "assignments",
        }
        or snapshot["phase"] != "completed"
        or snapshot["current"] is not None
    ):
        return None
    return snapshot


def _accepted(binding, completed, notices):
    if any(completed[k] != binding[k] for k in ("id", "leader", "workers")):
        return False
    coder, cleaner = notices
    if (
        coder["pack"] != binding["id"]
        or cleaner["pack"] != binding["id"]
        or coder["round"] != cleaner["round"]
        or binding["current"] != "coder:" + coder["round"]
        or (coder["role"], coder["status"], coder["decision"]) != ("coder", "done", "active")
        or (cleaner["role"], cleaner["status"], cleaner["decision"])
        != ("cleaner", "pass", "completed")
        or any(n["sender"] != binding["workers"][n["role"]] for n in notices)
    ):
        return False
    assignments = completed.get("assignments")
    if not isinstance(assignments, list) or len(assignments) != 2:
        return False
    receipts, receipt_ids = {}, set()
    for assignment in assignments:
        if not isinstance(assignment, dict):
            return False
        role = assignment.get("role")
        if role not in ("coder", "cleaner") or role in receipts:
            return False
        receipt = assignment.get("receipt")
        rejected = assignment.get("rejections")
        if (
            assignment.get("id") != f"{binding['id']}/{role}/{coder['round']}"
            or assignment.get("target") != binding["workers"][role]
            or not isinstance(receipt, dict)
            or type(receipt.get("id")) is not int
            or receipt["id"] <= 0
            or receipt.get("status") != ("done" if role == "coder" else "pass")
            or receipt.get("worker") != binding["workers"][role]
            or not isinstance(receipt.get("result"), dict)
            or not isinstance(receipt.get("notification"), dict)
            or receipt["notification"].get("status") != "delivered"
            or not isinstance(rejected, list)
            or any(
                not isinstance(r, dict) or type(r.get("id")) is not int or r["id"] <= 0
                for r in rejected
            )
        ):
            return False
        ids = [receipt["id"], *(r["id"] for r in rejected)]
        if len(set(ids)) != len(ids) or receipt_ids.intersection(ids):
            return False
        receipt_ids.update(ids)
        receipts[role] = receipt
    coder_result, cleaner_result = (receipts[role]["result"] for role in ("coder", "cleaner"))
    commits = (
        completed.get("baseline"),
        coder_result.get("input_commit"),
        coder_result.get("commit"),
        cleaner_result.get("input_commit"),
        cleaner_result.get("commit"),
    )
    return (
        all(isinstance(c, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", c) for c in commits)
        and commits[0] == commits[1]
        and commits[2] == commits[3]
    )


def continuation(turns, index, task, submitted_texts):
    """Return the actual final (id, text), or None if any boundary is uncertain."""
    task_id, development = task.get("id"), task.get("development_task")
    if not isinstance(development, str) or not development or not isinstance(task_id, str):
        return None
    prefix = (
        f"BRIDGE_TASK_ID={task_id}\nDevelopment task {development}; "
        "this is one round of the SAME task. Do not approve permission/trust dialogs, merge or deploy. "
        "Use your normal tools, extensions and skills for this task. "
    )
    if (
        not task["submitted_text"].startswith(prefix)
        or (task.get("recovery") or {}).get("state") == "restored"
        or index + 2 >= len(turns)
    ):
        return None
    original, coder, cleaner = turns[index : index + 3]
    if original["final"] or original["aborted"] or coder["final"] or coder["aborted"]:
        return None
    if any(t["text"] in submitted_texts for t in (coder, cleaner)):
        return None
    notices = [_NOTICE.fullmatch(t["text"]) for t in (coder, cleaner)]
    if not all(notices) or not cleaner["final"] or cleaner["aborted"]:
        return None
    if index + 3 < len(turns) and _NOTICE.fullmatch(turns[index + 3]["text"]):
        return None
    notices = [m.groupdict() for m in notices]
    try:
        answer = json.loads(
            cleaner["final"][1], object_pairs_hook=_unique, parse_constant=_constant
        )
    except (TypeError, ValueError, RecursionError):
        return None
    if (
        not isinstance(answer, dict)
        or answer.get("task_id") != task_id
        or answer.get("kind") != "result"
        or not isinstance(answer.get("answer"), dict)
    ):
        return None

    calls, seen_results, snapshots, final_count = {}, set(), [], 0
    for position, turn in enumerate((original, coder, cleaner)):
        for event in turn["events"]:
            role = event.get("role")
            if role == "system" or role == "assistant" and event.get("stopReason") == "aborted":
                return None
            if role == "assistant":
                content = event["content"]
                if (
                    event["stopReason"] == "stop"
                    and event.get("endTurn") is not False
                    and any(block.get("type") == "text" for block in content)
                    and not any(block.get("type") == "toolCall" for block in content)
                ):
                    final_count += 1
                    text = "".join(
                        block["text"] for block in content if block.get("type") == "text"
                    )
                    if position != 2 or text != cleaner["final"][1] or final_count != 1:
                        return None
                for block in content:
                    if block.get("type") == "toolCall":
                        call_id = block["id"]
                        if call_id in calls:
                            return None
                        command = _command(block["arguments"])
                        if command is not None and event.get("stopReason") != "toolUse":
                            return None
                        calls[call_id] = (block["name"], command)
            elif role == "toolResult":
                call_id = event["toolCallId"]
                if call_id in seen_results:
                    return None
                seen_results.add(call_id)
                call = calls.get(call_id)
                if call and call[0] == event["toolName"] == "bash" and call[1] is not None:
                    snapshot = _snapshot(event, call[1])
                    if snapshot is None:
                        return None
                    snapshots.append((position, call[1], snapshot))
    bindings = [
        (command, s)
        for position, command, s in snapshots
        if position == 0 and command[0] == "binding"
    ]
    completions = [(command, s) for _, command, s in snapshots if command[0] == "completed"]
    if len(bindings) != 1 or len(completions) != 1:
        return None
    binding_command, binding = bindings[0]
    completed_command, completed = completions[0]
    if binding_command[3:] != completed_command[3:]:
        return None
    if not _accepted(binding, completed, notices):
        return None
    return cleaner["final"]
