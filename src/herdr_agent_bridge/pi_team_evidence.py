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


def _path(value):
    return re.fullmatch(r"/[A-Za-z0-9_./-]+", value) is not None and all(
        part not in (".", "..") for part in value.split("/")
    )


def _command(arguments):
    """Recognize only the observed init and resume command forms; do not execute them."""
    if not isinstance(arguments, dict) or set(arguments) not in (
        {"command"},
        {"command", "timeout"},
    ):
        return None
    if "timeout" in arguments:
        timeout = arguments["timeout"]
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            return None
    command = arguments["command"]
    if not isinstance(command, str) or len(command) > 4096 or any(c in command for c in "`\\\n\r"):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None

    # The saved init used two literal local variables, not an arbitrary shell expansion.
    if len(tokens) == 22 and tokens[:3] == ["set", "-e", ";"]:
        root, helper = tokens[3], tokens[5]
        if (
            not root.startswith("root=")
            or not helper.startswith("helper=")
            or tokens[4] != ";"
            or tokens[6:9] != [";", "python3", "$helper"]
            or not _path(root[5:])
            or not _path(helper[7:])
        ):
            return None
        expanded = []
        for token in tokens[9:]:
            if token.startswith("$root/"):
                expanded.append(root[5:] + token[5:])
            elif "$" in token:
                return None
            else:
                expanded.append(token)
        tokens = ["python3", helper[7:], *expanded]
    elif any("$" in token for token in tokens):
        return None

    if len(tokens) == 15 and tokens[0] == "python3" and tokens[3:5] == ["two", "init"]:
        if (
            not _path(tokens[1])
            or not tokens[1].endswith("/scripts/handoff.py")
            or any(not _path(tokens[i]) for i in (2, 6, 8, 10))
            or [tokens[i] for i in (5, 7, 9, 11, 13)]
            != ["--project", "--card", "--checks", "--coder", "--cleaner"]
            or any(re.fullmatch(_ADDRESS, tokens[i]) is None for i in (12, 14))
            or tokens[12] == tokens[14]
        ):
            return None
        return "init", None, None, tokens[1], tokens[2], (tokens[6], tokens[12], tokens[14])
    if (
        len(tokens) == 6
        and tokens[0] == "python3"
        and _path(tokens[1])
        and tokens[1].endswith("/scripts/handoff.py")
        and _path(tokens[2])
        and tokens[3] == "two"
        and re.fullmatch(_PACK, tokens[4])
        and tokens[5] == "resume"
    ):
        return "direct", tokens[4], None, tokens[1], tokens[2], None

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
        or any(not _path(p) for p in tokens[8:10])
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
    return kind, tokens[11], tokens[2], tokens[8], tokens[9], None


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
            if len(values) > 256:
                return None
            text = text[end:]
    except (ValueError, RecursionError):
        return None
    return values if values and all(isinstance(v, dict) for v in values) else None


def _origin(message, command):
    objects = _objects(message)
    if objects is None or len(objects) != 1:
        return None
    origin = objects[0]
    _, _, _, _, store, _ = command
    if (
        set(origin)
        != {"id", "run", "directory", "database", "phase", "branch", "baseline", "card_version"}
        or not isinstance(origin["id"], str)
        or re.fullmatch(_PACK, origin["id"]) is None
        or origin["run"] != origin["id"]
        or origin["directory"] != store
        or origin["database"] != store.rstrip("/") + "/two-pack.sqlite3"
        or origin["phase"] != "ready"
        or not isinstance(origin["branch"], str)
        or not origin["branch"]
        or not isinstance(origin["baseline"], str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", origin["baseline"]) is None
        or type(origin["card_version"]) is not int
        or origin["card_version"] != 1
    ):
        return None
    return origin


def _snapshot(message, command):
    objects = _objects(message)
    if objects is None:
        return None
    kind, pack, waited, _, _, _ = command
    if kind == "direct":
        if len(objects) != 1:
            return None
        snapshot = objects[0]
        if (
            set(snapshot)
            != {
                "id",
                "phase",
                "leader",
                "workers",
                "current",
                "summary",
                "version",
                "pack",
                "pack_version",
                "project",
                "project_id",
                "rounds",
                "branch",
                "baseline",
                "assignments",
                "card_version",
                "card_versions",
                "card",
                "checks",
                "created_at",
                "updated_at",
            }
            or type(snapshot["version"]) is not int
            or snapshot["version"] != 1
            or snapshot["pack"] != "two"
            or type(snapshot["pack_version"]) is not int
            or snapshot["pack_version"] != 1
            or type(snapshot["rounds"]) is not int
            or snapshot["rounds"] < 1
            or not isinstance(snapshot["project"], str)
            or not isinstance(snapshot["assignments"], dict)
            or not isinstance(snapshot["card_versions"], list)
            or not isinstance(snapshot["card"], dict)
            or not isinstance(snapshot["checks"], dict)
            or type(snapshot["card_version"]) is not int
        ):
            return None
        assignments = snapshot["assignments"]
        for key, item in assignments.items():
            if (
                not isinstance(item, dict)
                or item.get("role") not in ("coder", "cleaner")
                or type(item.get("round")) is not int
                or item["round"] <= 0
                or key != f"{item['role']}:{item['round']}"
                or item.get("id") != f"{pack}/{item['role']}/{item['round']}"
            ):
                return None
        snapshot = {**snapshot, "assignments": list(assignments.values())}
        kind = "binding" if snapshot["phase"] == "active" else "completed"
        if kind == "binding" and (
            not isinstance(snapshot["current"], str) or snapshot["current"] not in assignments
        ):
            return None
        objects = None
    else:
        if len(objects) < 5:
            return None
        wait, snapshot, *terminals = objects
    if objects is not None and (
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
    ):
        return None
    if (
        snapshot.get("id") != pack
        or not isinstance(snapshot.get("workers"), dict)
        or set(snapshot["workers"]) != {"coder", "cleaner"}
        or any(
            not isinstance(x, str) or re.fullmatch(_ADDRESS, x) is None
            for x in (snapshot.get("leader"), *snapshot["workers"].values())
        )
    ):
        return None
    addresses = {snapshot["leader"], *snapshot["workers"].values()}
    if objects is None and any(
        item.get("target") != snapshot["workers"][item["role"]] for item in snapshot["assignments"]
    ):
        return None
    if len(addresses) != 3:
        return None
    if objects is not None and (
        any(
            set(t) != {"address", "status", "turn"}
            or not isinstance(t["address"], str)
            or not isinstance(t["status"], str)
            or type(t["turn"]) is not int
            for t in terminals
        )
        or not addresses.issubset({t["address"] for t in terminals})
        or len({t["address"] for t in terminals}) != len(terminals)
        or waited != snapshot["workers"]["coder" if kind == "binding" else "cleaner"]
    ):
        return None
    if objects is None:
        if kind == "completed" and (
            snapshot["phase"] != "completed" or snapshot["current"] is not None
        ):
            return None
    elif kind == "binding":
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
    return kind, snapshot


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
    # Plain resume exposes the assignment handoff as well as the two receipts.
    # A conflicting source or input commit cannot corroborate that handoff.
    if "project" in completed:
        for assignment in assignments:
            role = assignment["role"]
            source = assignment.get("source_receipt")
            if (
                "source_receipt" not in assignment
                or assignment.get("input_commit") != receipts[role]["result"].get("input_commit")
                or (role == "coder" and source is not None)
                or (
                    role == "cleaner"
                    and (type(source) is not int or source != receipts["coder"]["id"])
                )
            ):
                return False
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


def continuation(turns, index, task, submitted_texts, cwd):
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

    calls, seen_results, recognized_results, snapshots, final_count = {}, set(), set(), [], 0
    sequence = 0
    for position, turn in enumerate((original, coder, cleaner)):
        for event in turn["events"]:
            sequence += 1
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
                        raw = block["arguments"].get("command")
                        if (
                            command is None
                            and isinstance(raw, str)
                            and re.search(r"\btwo\s+init\b", raw)
                        ):
                            return None
                        if command is not None and (
                            event.get("stopReason") != "toolUse" or block["name"] != "bash"
                        ):
                            return None
                        calls[call_id] = (block["name"], command, sequence)
            elif role == "toolResult":
                call_id = event["toolCallId"]
                if call_id in seen_results:
                    return None
                seen_results.add(call_id)
                call = calls.get(call_id)
                if call and call[0] == event["toolName"] == "bash" and call[1] is not None:
                    command = call[1]
                    if command[0] == "init":
                        origin = _origin(event, command)
                        snapshot = ("init", origin) if origin is not None else None
                    else:
                        snapshot = _snapshot(event, command)
                    if snapshot is None:
                        return None
                    kind, data = snapshot
                    recognized_results.add(call_id)
                    snapshots.append((position, kind, command, data, call[2], sequence))
    if recognized_results != {call_id for call_id, call in calls.items() if call[1] is not None}:
        return None
    init_calls = [call for call in calls.values() if call[1] is not None and call[1][0] == "init"]
    origins = [item for item in snapshots if item[1] == "init"]
    bindings = [item for item in snapshots if item[1] == "binding"]
    completions = [item for item in snapshots if item[1] == "completed"]
    if len(init_calls) != 1 or len(origins) != 1 or len(bindings) != 1 or len(completions) != 1:
        return None
    origin_pos, _, origin_command, origin, _, origin_end = origins[0]
    binding_pos, _, binding_command, binding, binding_start, binding_end = bindings[0]
    _, _, completed_command, completed, completed_start, _ = completions[0]
    project, coder_worker, cleaner_worker = origin_command[5]
    if (
        origin_pos != 0
        or binding_pos != 0
        or not origin_end < binding_start <= binding_end < completed_start
        or origin_command[3:5] != binding_command[3:5]
        or origin_command[3:5] != completed_command[3:5]
        or project != cwd
        or origin["id"] != binding["id"]
        or origin["id"] != completed["id"]
        or binding["workers"] != {"coder": coder_worker, "cleaner": cleaner_worker}
        or any(
            snapshot.get("branch", origin["branch"]) != origin["branch"]
            or snapshot.get("baseline", origin["baseline"]) != origin["baseline"]
            or snapshot.get("project", project) != project
            for snapshot in (binding, completed)
        )
    ):
        return None
    if not _accepted(binding, completed, notices):
        return None
    return cleaner["final"]
