"""Disposable Pi-format fixtures; these tests do not run Pi or hc."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_agent_bridge import pi_evidence as pi_evidence

STAMP = "2026-11-06T00:00:00.000Z"
THREAD = "a1901e11-820c-4473-aa3a-df22aeb17000"
SELECTION = {"session_id": THREAD, "model": "fixture-provider/fixture-model", "thinking": "high"}


def header():
    return {
        "type": "session",
        "version": 3,
        "id": THREAD,
        "timestamp": STAMP,
        "cwd": "/fixture/project",
    }


def entry(identifier, parent, role, content, **extra):
    message = {"role": role, "content": content, "timestamp": 1793923200000, **extra}
    return {
        "type": "message",
        "id": identifier,
        "parentId": parent,
        "timestamp": STAMP,
        "message": message,
    }


def user(identifier, parent, text):
    return entry(identifier, parent, "user", text)


def assistant(identifier, parent, content, stop="stop", **extra):
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    return entry(
        identifier,
        parent,
        "assistant",
        content,
        api="fixture-api",
        provider="fixture-provider",
        model="fixture-model",
        usage={},
        stopReason=stop,
        **extra,
    )


def task(identifier, text):
    return {"id": identifier, "submitted_text": text}


def selected_prefix():
    return [
        header(),
        {
            "type": "model_change",
            "id": "m",
            "parentId": None,
            "timestamp": STAMP,
            "provider": "fixture-provider",
            "modelId": "fixture-model",
        },
        {
            "type": "thinking_level_change",
            "id": "level",
            "parentId": "m",
            "timestamp": STAMP,
            "thinkingLevel": "high",
        },
    ]


PACK = "a" * 32


def native_development_task(identifier="lead"):
    text = (
        f"BRIDGE_TASK_ID={identifier}\nDevelopment task work; this is one round of the SAME task. "
        "Do not approve permission/trust dialogs, merge or deploy. "
        "Use your normal tools, extensions and skills for this task. Goal: fixture"
    )
    return {**task(identifier, text), "development_task": "work"}


def pack_initialization(initialized_pack=PACK):
    origin = {
        "id": initialized_pack,
        "run": initialized_pack,
        "directory": "/fixture/store",
        "database": "/fixture/store/two-pack.sqlite3",
        "phase": "ready",
        "branch": "fixture",
        "baseline": "b" * 40,
        "card_version": 1,
    }
    return [
        assistant(
            "init",
            None,
            [
                {
                    "type": "toolCall",
                    "id": "init-call",
                    "name": "bash",
                    "arguments": {
                        "command": "python3 /fixture/scripts/handoff.py /fixture/store two init "
                        "--project /fixture/project --card /fixture/card --checks /fixture/checks "
                        "--coder worker1 --cleaner worker2",
                        "timeout": 20,
                    },
                }
            ],
            "toolUse",
        ),
        entry(
            "init-result",
            None,
            "toolResult",
            [{"type": "text", "text": json.dumps(origin)}],
            toolCallId="init-call",
            toolName="bash",
            isError=False,
        ),
    ]


def two_pack_history(submitted, task_id="lead", kind="result"):
    """Synthetic transcript shaped like the observed two-pack tool output."""
    workers = {"coder": "worker1", "cleaner": "worker2"}
    binding = {
        "id": PACK,
        "phase": "active",
        "leader": "lead-terminal",
        "workers": workers,
        "current": "coder:1",
        "summary": "active",
        "round_count": 1,
    }
    assignments = [
        {
            "id": f"{PACK}/{role}/1",
            "role": role,
            "target": worker,
            "rejections": [],
            "receipt": {
                "id": receipt_id,
                "status": status,
                "worker": worker,
                "result": {"input_commit": old, "commit": new},
                "notification": {"status": "delivered"},
            },
        }
        for role, worker, receipt_id, status, old, new in (
            ("coder", "worker1", 1, "done", "b" * 40, "c" * 40),
            ("cleaner", "worker2", 2, "pass", "c" * 40, "d" * 40),
        )
    ]
    # A rejected earlier attempt is not the later accepted Cleaner receipt.
    assignments[1]["rejections"] = [{"id": 7, "status": "pass", "worker": "worker2"}]
    completed = {
        "id": PACK,
        "phase": "completed",
        "leader": "lead-terminal",
        "workers": workers,
        "branch": "fixture",
        "baseline": "b" * 40,
        "current": None,
        "summary": "completed",
        "assignments": assignments,
    }
    tails = [
        {"address": name, "status": "working", "turn": 0}
        for name in ("lead-terminal", "worker1", "worker2")
    ]

    def call(identifier, phase):
        filt = (
            "{id,phase,leader,workers,current,summary,round_count:(.rounds|length)}"
            if phase == "active"
            else "{id,phase,leader,workers,branch,baseline,current,summary,"
            "assignments:[.assignments[]|{id,role,target,rejections,receipt}]}"
        )
        worker = workers["coder" if phase == "active" else "cleaner"]
        command = (
            ("set -o pipefail; " if phase == "active" else "")
            + f"hc wait {worker} --timeout 20 --json; "
            + f"python3 /fixture/scripts/handoff.py /fixture/store two {PACK} resume | jq '{filt}'; "
            + "hc ls --json | jq -c '.terminals[] | {address,status,turn}'"
        )
        return assistant(
            identifier,
            None,
            [
                {
                    "type": "toolCall",
                    "id": identifier + "-call",
                    "name": "bash",
                    "arguments": {"command": command, "timeout": 26},
                }
            ],
            "toolUse",
        )

    def output(identifier, call_id, phase):
        worker = workers["coder" if phase == "active" else "cleaner"]
        wait = {
            "children": [],
            "ended": False,
            "exit_code": None,
            "reason": "timeout",
            "signal": None,
            "status": "working",
            "terminal": worker,
            "turn": 0,
        }
        content = "\n".join(
            json.dumps(obj) for obj in [wait, binding if phase == "active" else completed, *tails]
        )
        return entry(
            identifier,
            None,
            "toolResult",
            [{"type": "text", "text": content}],
            toolCallId=call_id + "-call",
            toolName="bash",
            isError=False,
        )

    def notice(identifier, role, worker, status, decision):
        return user(
            identifier,
            None,
            f'<hc from="{worker} fixture-{role}">Herdr two-pack receipt saved for {PACK}/{role}/1 ({status}). Decision: {decision}. Inspect with python3 /ignored/tool.py /ignored/store two {PACK} resume.</hc>',
        )

    records = (
        selected_prefix()
        + [user("original", None, submitted)]
        + pack_initialization()
        + [
            call("binding-call", "active"),
            output("binding-output", "binding-call", "active"),
            notice("coder-notice", "coder", "worker1", "done", "active"),
            call("complete-call", "completed"),
            output("complete-output", "complete-call", "completed"),
            notice("cleaner-notice", "cleaner", "worker2", "pass", "completed"),
            assistant(
                "final",
                None,
                json.dumps({"task_id": task_id, "kind": kind, "answer": {"summary": "done"}}),
            ),
        ]
    )
    return relink(records)


def retry_pack_history(submitted, task_id="lead"):
    """Failed preflight, guarded init, dispatch and independently projected receipts."""
    records = two_pack_history(submitted, task_id)
    by_id = {item["id"]: item for item in records}
    helper = "/fixture/scripts/handoff.py"
    store = '"$(realpath ../store)"'
    init_suffix = (
        f'python3 "$helper" {store} two init --project "$PWD" '
        '--card "$(realpath ../card)" --checks "$(realpath ../checks)" '
        "--coder worker1 --cleaner worker2"
    )
    preflight = (
        'set -e; for peer in worker1 worker2; do proc=$(hc get "$peer" --json | jq -r .foreground.pid); '
        'python3 - "$proc" "$peer" <<\'GUARD\'\n'
        "# Synthetic, read-only guard.\n"
        'for label in ("READY", "BLOCK"):\n'
        '    assert label != "BLOCK", label\n'
        "GUARD\n"
        f'done; test  -z "$(git status --porcelain)"; python3 {helper} "$PWD/../store" '
        'two init --project "$PWD" --card "$PWD/../card" --checks "$PWD/../checks" '
        "--coder worker1 --cleaner worker2"
    )

    def call(identifier, command):
        return assistant(
            identifier,
            None,
            [
                {
                    "type": "toolCall",
                    "id": identifier + "-call",
                    "name": "bash",
                    "arguments": {"command": command},
                }
            ],
            "toolUse",
        )

    def output(identifier, call_id, text, error=False):
        return entry(
            identifier,
            None,
            "toolResult",
            [{"type": "text", "text": text}],
            toolCallId=call_id + "-call",
            toolName="bash",
            isError=error,
        )

    preflight_result = (
        'Traceback (most recent call last):\n  File "<stdin>", line 3, in <module>\n'
        "AssertionError: BLOCK\n\n\nCommand exited with code 1"
    )
    index = records.index(by_id["init"])
    records[index:index] = [
        call("preflight", preflight),
        output("preflight-output", "preflight", preflight_result, True),
    ]
    by_id["init"]["message"]["content"][0]["arguments"]["command"] = (
        f'set -e; test -z "$(git status --porcelain)"; helper={helper}; {init_suffix}'
    )
    dispatch = {
        "assignment": f"{PACK}/coder/1",
        "target": "worker1",
        "delivery": {"number": 1, "status": "delivered"},
        "receipt_saved": False,
    }
    index = records.index(by_id["binding-call"])
    records[index:index] = [
        call(
            "start",
            f'set -e -o pipefail; helper={helper}; python3 "$helper" {store} two {PACK} start | jq -c .',
        ),
        output("start-output", "start", json.dumps(dispatch)),
    ]
    wait = {
        "children": [],
        "ended": False,
        "exit_code": None,
        "reason": "timeout",
        "signal": None,
        "status": "working",
        "terminal": "worker1",
        "turn": 0,
    }
    binding = {
        "phase": "active",
        "leader": "lead-terminal",
        "workers": {"coder": "worker1", "cleaner": "worker2"},
        "current": "coder:1",
        "summary": "active",
        "rounds": [],
    }
    by_id["binding-call"]["message"]["content"][0]["arguments"]["command"] = (
        f'hc wait worker1 --timeout 20 --json; helper={helper}; python3 "$helper" {store} '
        f"two {PACK} resume | jq -c '{{phase,leader,workers,current,summary,rounds:[.rounds[]? | {{role,round,status}}]}}'"
    )
    by_id["binding-output"]["message"]["content"][0]["text"] = "\n".join(
        map(json.dumps, [wait, binding])
    )
    receipts = [
        {
            "role": role,
            "round": 1,
            "target": worker,
            "worker": worker,
            "receipt_id": receipt_id,
            "status": status,
            "commit": "c" * 40,
            "checks": [],
        }
        for role, worker, receipt_id, status in (
            ("coder", "worker1", 1, "done"),
            ("cleaner", "worker2", 3, "pass"),
        )
    ]
    completed = {
        "id": PACK,
        "phase": "completed",
        "leader": "lead-terminal",
        "workers": binding["workers"],
        "branch": "fixture",
        "receipts": receipts,
    }
    by_id["complete-call"]["message"]["content"][0]["arguments"]["command"] = (
        f"set -euo pipefail; helper={helper}; store=$(realpath ../store); run={PACK}; "
        'receipt=$(python3 "$helper" "$store" two "$run" resume); '
        "printf '%s\\n' \"$receipt\" | jq -ce '{id,phase,leader,workers,branch,receipts:[.assignments[] | {role,round,target,worker:.receipt.worker,receipt_id:.receipt.id,status:.receipt.status,commit:.receipt.result.commit,checks:.receipt.result.checks}]}'; "
        'test -n "$receipt"'
    )
    by_id["complete-output"]["message"]["content"][0]["text"] = (
        json.dumps(completed) + "\nfixture checks passed\n"
    )
    final = {
        "task_id": task_id,
        "kind": "result",
        "answer": {
            "status": "completed",
            "pack_id": PACK,
            "lead_id": "lead-terminal",
            "branch": "fixture",
            "checked_commit": "c" * 40,
            "signoffs": {
                role: {"terminal_id": worker, "receipt_id": receipt_id, "status": status}
                for role, worker, receipt_id, status in (
                    ("coder", "worker1", 1, "done"),
                    ("cleaner", "worker2", 3, "pass"),
                )
            },
        },
    }
    by_id["final"]["message"]["content"][0]["text"] = json.dumps(final)
    for identifier in ("complete-call", "complete-output"):
        records.remove(by_id[identifier])
    index = records.index(by_id["final"])
    records[index:index] = [by_id["complete-call"], by_id["complete-output"]]
    return relink(records)


def fixture_verification_tail():
    """Independently authored, synthetic post-projection verification shape."""
    return (
        "commit=$(jq -er '[.assignments[].receipt.result.commit] | unique' <<< \"$receipt\"); "
        'test  "$(git rev-parse HEAD)" = "$commit"; test  -z "$(git status --porcelain)"; '
        f'git diff --name-only {"b" * 40} "$commit"; '
        "python3 - <<'CHECK'\n"
        "import subprocess\n"
        'for options in ([], ["--dry-run"]):\n'
        '    argv = ["python3", "fixture_program.py", *options]\n'
        "    observed = subprocess.run(argv, capture_output=True, text=True, timeout=4)\n"
        "    assert observed.returncode == 0\n"
        "CHECK\n"
        "timeout 15 python3 -m unittest discover -s tests -v; "
        'test  "$(git rev-parse HEAD)" = "$commit"; test  -z "$(git status --porcelain)"'
    )


def relink(records):
    for index, record in enumerate(records[1:], 1):
        record["parentId"] = records[index - 1]["id"] if index > 1 else None
    return records


class PiEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hab-pi-evidence-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "pi"
        self.path = self.home / "sessions" / "--fixture-project--" / "session.jsonl"
        self.path.parent.mkdir(parents=True)

    def write(self, entries, tail=b""):
        self.path.write_bytes(
            b"".join(
                json.dumps(item, ensure_ascii=False).encode("utf-8") + b"\n" for item in entries
            )
            + tail
        )

    def evidence(self, entries, tasks, tail=b""):
        self.write(entries, tail)
        return pi_evidence.extract_turns(self.home, tasks)

    def test_native_development_correlates_independently_corroborated_two_pack(self):
        submitted = native_development_task()
        entries = two_pack_history(submitted["submitted_text"])
        self.write(entries)
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"]["agent_turn_id"], "original")
        self.assertEqual(result["lead"]["final_message_id"], "final")
        self.assertEqual(json.loads(result["lead"]["answer"])["kind"], "result")
        self.assertFalse(
            pi_evidence.extract_turns(self.home, [submitted])["lead"]["agent_completed"]
        )
        # The acceptance snapshot can also be observed after both notifications.
        after_both = copy.deepcopy(entries)
        cleaner_notice = next(e for e in after_both if e["id"] == "cleaner-notice")
        after_both.remove(cleaner_notice)
        before_call = next(i for i, e in enumerate(after_both) if e["id"] == "complete-call")
        after_both.insert(before_call, cleaner_notice)
        self.write(relink(after_both))
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"]["final_message_id"], "final")

    def test_native_two_pack_retry_preserves_original_and_final_provenance(self):
        submitted = native_development_task()
        self.write(retry_pack_history(submitted["submitted_text"]))
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        evidence = result["lead"]
        self.assertTrue(evidence["agent_completed"])
        self.assertEqual(evidence["agent_turn_id"], "original")
        self.assertEqual(evidence["final_message_id"], "final")
        self.assertEqual(evidence["turn_id_source"], "pi_user_message_id")
        answer = json.loads(evidence["answer"])
        self.assertEqual(answer["task_id"], "lead")
        self.assertEqual(answer["answer"]["pack_id"], PACK)
        self.assertEqual(answer["answer"]["checked_commit"], "c" * 40)

    def test_retry_accepts_reordered_declarative_projections(self):
        submitted = native_development_task()
        records = retry_pack_history(submitted["submitted_text"])
        by_id = {record["id"]: record for record in records}
        binding = by_id["binding-call"]["message"]["content"][0]["arguments"]
        binding["command"] = binding["command"].replace(
            "{phase,leader,workers,current,summary,rounds:[.rounds[]? | {role,round,status}]}",
            "{current, workers, phase, leader}",
        )
        output = by_id["binding-output"]["message"]["content"][0]
        wait, snapshot = map(json.loads, output["text"].splitlines())
        snapshot.pop("rounds")
        snapshot.pop("summary")
        output["text"] = "\n".join(map(json.dumps, (wait, snapshot)))
        completion = by_id["complete-call"]["message"]["content"][0]["arguments"]
        completion["command"] = completion["command"].replace(
            "{id,phase,leader,workers,branch,receipts:[.assignments[] | {role,round,target,worker:.receipt.worker,receipt_id:.receipt.id,status:.receipt.status,commit:.receipt.result.commit,checks:.receipt.result.checks}]}",
            "{branch,workers,leader,phase,id,receipts:[.assignments[] | {checks:.receipt.result.checks,commit:.receipt.result.commit,status:.receipt.status,receipt_id:.receipt.id,worker:.receipt.worker,target,round,role}]}",
        )
        self.write(records)
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"]["final_message_id"], "final")

    def test_retry_allows_horizontal_spacing_between_guard_words(self):
        submitted = native_development_task()
        for ident in ("preflight", "init"):
            with self.subTest(ident=ident):
                records = retry_pack_history(submitted["submitted_text"])
                call = next(r for r in records if r["id"] == ident)
                args = call["message"]["content"][0]["arguments"]
                args["command"] = args["command"].replace("set -e;", "set  -e;", 1)
                self.write(records)
                result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
                self.assertEqual(result["lead"].get("final_message_id"), "final")

    def test_guarded_init_accepts_only_horizontal_separation_between_argv_words(self):
        submitted = native_development_task()
        for separator, expected in (("  ", True), ("\t", True), ("\n", False), (";", False)):
            with self.subTest(separator=separator):
                records = retry_pack_history(submitted["submitted_text"])
                call = next(r for r in records if r["id"] == "init")
                args = call["message"]["content"][0]["arguments"]
                args["command"] = args["command"].replace("two init", f"two{separator}init", 1)
                self.write(records)
                result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
                self.assertEqual(
                    result["lead"].get("final_message_id"), "final" if expected else None
                )

    def test_two_pack_unrelated_read_only_filename_is_not_a_pack_command(self):
        submitted = native_development_task()
        records = two_pack_history(submitted["submitted_text"])
        call, output = [
            copy.deepcopy(next(r for r in records if r["id"] == key))
            for key in ("init", "init-result")
        ]
        call["id"], output["id"] = "read-call", "read-result"
        block = call["message"]["content"][0]
        block.update(id="read-file", arguments={"command": "wc -l docs/handoff.py.md"})
        output["message"].update(
            toolCallId="read-file", content=[{"type": "text", "text": "5 docs/handoff.py.md"}]
        )
        index = next(i for i, r in enumerate(records) if r["id"] == "binding-call")
        records[index:index] = [call, output]
        self.write(relink(records))
        result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(result["lead"].get("final_message_id"), "final")

    def test_retry_orphan_result_indicating_another_origin_is_not_ignorable(self):
        submitted = native_development_task()
        records = retry_pack_history(submitted["submitted_text"])
        index = next(i for i, r in enumerate(records) if r["id"] == "init-result")
        orphan = copy.deepcopy(records[index])
        orphan["id"] = "orphan-result"
        orphan["message"]["toolCallId"] = "missing-call"
        block = orphan["message"]["content"][0]
        origin = json.loads(block["text"])
        block["text"] = json.dumps({**origin, "id": "d" * 32, "run": "d" * 32})
        records.insert(index + 1, orphan)
        self.write(relink(records))
        result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertFalse(result["lead"]["agent_completed"], "unpaired competing origin")

    def test_retry_accepts_bounded_post_projection_verification(self):
        submitted = native_development_task()
        records = retry_pack_history(submitted["submitted_text"])
        call = next(r for r in records if r["id"] == "complete-call")
        args = call["message"]["content"][0]["arguments"]
        args["command"] = (
            args["command"].removesuffix('test -n "$receipt"') + fixture_verification_tail()
        )
        self.write(records)
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"].get("final_message_id"), "final")

        for label, change in (
            ("another shell command", lambda s: s + "; echo unexpected"),
            ("arbitrary jq input", lambda s: s.replace(" | unique'", " | inputs'")),
            ("helper as program", lambda s: s.replace("fixture_program.py", "handoff.py")),
            ("option is not a script", lambda s: s.replace("fixture_program.py", "-fixture.py")),
            (
                "premature heredoc end",
                lambda s: s.replace("import subprocess", "CHECK\nimport subprocess"),
            ),
            ("arbitrary python", lambda s: s.replace("import subprocess", "import subprocess, os")),
            ("shell execution", lambda s: s.replace("capture_output=True", "shell=True")),
        ):
            with self.subTest(label=label):
                changed = copy.deepcopy(records)
                item = next(r for r in changed if r["id"] == "complete-call")
                arguments = item["message"]["content"][0]["arguments"]
                arguments["command"] = change(arguments["command"])
                self.write(changed)
                result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
                self.assertFalse(result["lead"]["agent_completed"], label)

    def test_retry_preflight_assertion_uses_its_actual_source_line(self):
        submitted = native_development_task()
        records = retry_pack_history(submitted["submitted_text"])
        call = next(r for r in records if r["id"] == "preflight")
        args = call["message"]["content"][0]["arguments"]
        args["command"] = args["command"].replace(
            "# Synthetic, read-only guard.\n", "# A different guard layout.\n\n"
        )
        output = next(r for r in records if r["id"] == "preflight-output")
        block = output["message"]["content"][0]
        block["text"] = block["text"].replace("line 3", "line 4")
        self.write(records)
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"].get("final_message_id"), "final")

    def test_retry_without_failed_preflight_still_correlates(self):
        submitted = native_development_task()
        records = retry_pack_history(submitted["submitted_text"])
        records = [r for r in records if r["id"] not in ("preflight", "preflight-output")]
        self.write(relink(records))
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"]["final_message_id"], "final")

    def test_retry_rejects_ambiguous_ownership_and_uncorroborated_receipts(self):
        submitted = native_development_task()
        valid = retry_pack_history(submitted["submitted_text"])

        def record(items, ident):
            return next(item for item in items if item["id"] == ident)

        def command(items, ident, change):
            arguments = record(items, ident)["message"]["content"][0]["arguments"]
            arguments["command"] = change(arguments["command"])

        def output(items, ident, index, change):
            block = record(items, ident)["message"]["content"][0]
            lines = block["text"].splitlines()
            value = json.loads(lines[index])
            change(value)
            lines[index] = json.dumps(value)
            block["text"] = "\n".join(lines) + "\n"

        def answer(items, change):
            output(items, "final", 0, lambda value: change(value["answer"]))

        def duplicate_init(items):
            call = copy.deepcopy(record(items, "init"))
            result = copy.deepcopy(record(items, "init-result"))
            call["id"] = "second-init"
            call["message"]["content"][0]["id"] = "second-init-call"
            result["id"] = "second-init-result"
            result["message"]["toolCallId"] = "second-init-call"
            index = items.index(record(items, "start"))
            items[index:index] = [call, result]

        def add_unresolved_init(items):
            item = copy.deepcopy(record(items, "init"))
            item["id"] = "unresolved-init"
            item["message"]["content"][0]["id"] = "unresolved-init-call"
            items.insert(items.index(record(items, "start")), item)

        def move(items, ident, after):
            item = record(items, ident)
            items.remove(item)
            items.insert(items.index(record(items, after)) + 1, item)

        cases = {
            "not the preflight error": lambda r: record(r, "preflight-output")["message"][
                "content"
            ][0].update(text="Command exited with code 1"),
            "unexplained output before the failed guard": lambda r: record(r, "preflight-output")[
                "message"
            ]["content"][0].update(
                text=record(r, "init-result")["message"]["content"][0]["text"].strip()
                + "\n"
                + record(r, "preflight-output")["message"]["content"][0]["text"]
            ),
            "preflight impossible source line length": lambda r: record(r, "preflight-output")[
                "message"
            ]["content"][0].update(
                text=record(r, "preflight-output")["message"]["content"][0]["text"].replace(
                    "line 3", "line " + "9" * 5000
                )
            ),
            "preflight wrong source line": lambda r: record(r, "preflight-output")["message"][
                "content"
            ][0].update(
                text=record(r, "preflight-output")["message"]["content"][0]["text"].replace(
                    "line 3", "line 2"
                )
            ),
            "preflight unrelated assertion": lambda r: record(r, "preflight-output")["message"][
                "content"
            ][0].update(
                text=record(r, "preflight-output")["message"]["content"][0]["text"].replace(
                    "AssertionError: BLOCK", "AssertionError: OTHER"
                )
            ),
            "preflight succeeded": lambda r: record(r, "preflight-output")["message"].update(
                isError=False
            ),
            "unpaired preflight": lambda r: record(r, "preflight-output")["message"].update(
                toolCallId="other"
            ),
            "preflight body changed": lambda r: command(
                r,
                "preflight",
                lambda s: s.replace(
                    '    assert label != "BLOCK", label',
                    '    __import__("os").system("true")\n    assert label != "BLOCK", label',
                ),
            ),
            "no registration": lambda r: r.__setitem__(
                slice(None), [x for x in r if x["id"] not in ("init", "init-result")]
            ),
            "no independent dispatch": lambda r: r.__setitem__(
                slice(None), [x for x in r if x["id"] not in ("start", "start-output")]
            ),
            "no worker binding": lambda r: r.__setitem__(
                slice(None), [x for x in r if x["id"] not in ("binding-call", "binding-output")]
            ),
            "preflight may modify environment": lambda r: command(
                r,
                "preflight",
                lambda s: s.replace(
                    'for label in ("READY", "BLOCK"):',
                    'import os\nvalues = (v for os.environ["X"] in ("Y",))\nfor label in ("READY", "BLOCK"):',
                ),
            ),
            "preflight output inside assertion": lambda r: command(
                r,
                "preflight",
                lambda s: s.replace(
                    'assert label != "BLOCK", label',
                    'assert print("not-a-guard") == label, label',
                ),
            ),
            "preflight fake multi-line output": lambda r: command(
                r,
                "preflight",
                lambda s: s.replace(
                    "# Synthetic, read-only guard.", 'print("first\\nsecond\\nthird")'
                ),
            ),
            "preflight may run init before failure": lambda r: command(
                r,
                "preflight",
                lambda s: s.replace(
                    "set -e; for",
                    "python3 /fixture/scripts/handoff.py /fixture/store two init; false; for",
                ),
            ),
            "failed preflight store differs": lambda r: command(
                r, "preflight", lambda s: s.replace('"$PWD/../store"', '"$PWD/../other"')
            ),
            "failed preflight worker differs": lambda r: command(
                r, "preflight", lambda s: s.replace("worker2", "other")
            ),
            "origin output other store": lambda r: output(
                r,
                "init-result",
                0,
                lambda x: x.update(
                    directory="/elsewhere/store", database="/elsewhere/store/two-pack.sqlite3"
                ),
            ),
            "other native project": lambda r: r[0].update(cwd="/other/project"),
            "init failed after creating pack": lambda r: record(r, "init-result")["message"].update(
                isError=True
            ),
            "init error then successful-looking output": lambda r: record(r, "init-result")[
                "message"
            ]["content"][0].update(text="Command exited with code 1"),
            "extra shell after init": lambda r: command(r, "init", lambda s: s + "; false"),
            "second successful origin": duplicate_init,
            "second unresolved origin": add_unresolved_init,
            "unrecognized second origin": lambda r: command(
                r, "start", lambda s: s + '; python3 "$helper" "$store" two init'
            ),
            "store differs at dispatch": lambda r: command(
                r, "start", lambda s: s.replace("../store", "../other")
            ),
            "helper differs at binding": lambda r: command(
                r,
                "binding-call",
                lambda s: s.replace("/fixture/scripts/handoff.py", "/other/scripts/handoff.py"),
            ),
            "dispatch other pack": lambda r: output(
                r, "start-output", 0, lambda x: x.update(assignment=f"{'f' * 32}/coder/1")
            ),
            "dispatch other worker": lambda r: output(
                r, "start-output", 0, lambda x: x.update(target="other")
            ),
            "dispatch not delivered": lambda r: output(
                r, "start-output", 0, lambda x: x["delivery"].update(status="pending")
            ),
            "dispatch output not paired": lambda r: record(r, "start-output")["message"].update(
                toolCallId="other"
            ),
            "dispatch output is error": lambda r: record(r, "start-output")["message"].update(
                isError=True
            ),
            "dispatch after notice": lambda r: move(r, "start-output", "coder-notice"),
            "binding mismatched pack": lambda r: command(
                r, "binding-call", lambda s: s.replace(PACK, "f" * 32)
            ),
            "binding wrong worker": lambda r: output(
                r, "binding-output", 1, lambda x: x["workers"].update(coder="other")
            ),
            "binding wrong current": lambda r: output(
                r, "binding-output", 1, lambda x: x.update(current="cleaner:1")
            ),
            "binding printed JSON": lambda r: command(
                r, "binding-call", lambda s: "echo fabricated"
            ),
            "binding arbitrary jq": lambda r: command(
                r,
                "binding-call",
                lambda s: s.replace(
                    "{phase,leader,workers,current,summary,rounds:[.rounds[]? | {role,round,status}]}",
                    "{phase,leader,workers,current:env.fake}",
                ),
            ),
            "binding printed by inventory": lambda r: command(
                r,
                "binding-call",
                lambda s: s + "; hc ls --json | jq -c '{phase,leader,workers,current}'",
            ),
            "binding output extra field": lambda r: output(
                r, "binding-output", 1, lambda x: x.update(id="f" * 32)
            ),
            "unresolved unrelated call": lambda r: r.insert(
                r.index(record(r, "binding-call")),
                assistant(
                    "unresolved-read",
                    None,
                    [
                        {
                            "type": "toolCall",
                            "id": "unresolved-read-call",
                            "name": "read",
                            "arguments": {"path": "/fixture/readme"},
                        }
                    ],
                    "toolUse",
                ),
            ),
            "binding output not paired": lambda r: record(r, "binding-output")["message"].update(
                toolName="read"
            ),
            "binding after notice": lambda r: move(r, "binding-output", "coder-notice"),
            "missing receipt": lambda r: output(
                r, "complete-output", 0, lambda x: x["receipts"].pop()
            ),
            "duplicate role": lambda r: output(
                r, "complete-output", 0, lambda x: x["receipts"][1].update(role="coder")
            ),
            "duplicate receipt": lambda r: output(
                r, "complete-output", 0, lambda x: x["receipts"][1].update(receipt_id=1)
            ),
            "wrong round": lambda r: output(
                r, "complete-output", 0, lambda x: x["receipts"][1].update(round=2)
            ),
            "wrong worker": lambda r: output(
                r, "complete-output", 0, lambda x: x["receipts"][1].update(worker="other")
            ),
            "wrong target": lambda r: output(
                r, "complete-output", 0, lambda x: x["receipts"][0].update(target="other")
            ),
            "wrong status": lambda r: output(
                r, "complete-output", 0, lambda x: x["receipts"][1].update(status="rejected")
            ),
            "receipt commit differs": lambda r: output(
                r, "complete-output", 0, lambda x: x["receipts"][1].update(commit="f" * 40)
            ),
            "check commit differs": lambda r: output(
                r,
                "complete-output",
                0,
                lambda x: x["receipts"][1].update(checks=[{"commit": "f" * 40}]),
            ),
            "receipt output wrong pack": lambda r: output(
                r, "complete-output", 0, lambda x: x.update(id="f" * 32)
            ),
            "receipt output wrong leader": lambda r: output(
                r, "complete-output", 0, lambda x: x.update(leader="other")
            ),
            "receipt output wrong branch": lambda r: output(
                r, "complete-output", 0, lambda x: x.update(branch="other")
            ),
            "receipt output is error": lambda r: record(r, "complete-output")["message"].update(
                isError=True
            ),
            "receipt output unpaired": lambda r: record(r, "complete-output")["message"].update(
                toolCallId="other"
            ),
            "receipt JSON only printed": lambda r: command(
                r, "complete-call", lambda s: "echo fabricated"
            ),
            "receipt projection fabricates worker": lambda r: command(
                r,
                "complete-call",
                lambda s: s.replace("worker:.receipt.worker", 'worker:"worker1"'),
            ),
            "unbounded receipt tail": lambda r: command(
                r, "complete-call", lambda s: s + "; echo unexpected"
            ),
            "second init in receipt tail": lambda r: command(
                r, "complete-call", lambda s: s + '; python3 "$helper" "$store" two init'
            ),
            "second receipt projection": lambda r: record(r, "complete-output")["message"][
                "content"
            ][0].update(
                text=(
                    record(r, "complete-output")["message"]["content"][0]["text"].splitlines()[0]
                    + "\n"
                )
                * 2
            ),
            "final other pack": lambda r: answer(r, lambda x: x.update(pack_id="f" * 32)),
            "final other commit": lambda r: answer(r, lambda x: x.update(checked_commit="f" * 40)),
            "final other leader": lambda r: answer(r, lambda x: x.update(lead_id="other")),
            "final other receipt": lambda r: answer(
                r, lambda x: x["signoffs"]["cleaner"].update(receipt_id=2)
            ),
            "final other worker": lambda r: answer(
                r, lambda x: x["signoffs"]["coder"].update(terminal_id="other")
            ),
            "final task differs": lambda r: output(
                r, "final", 0, lambda x: x.update(task_id="other")
            ),
            "question instead of result": lambda r: output(
                r, "final", 0, lambda x: x.update(kind="question")
            ),
            "independent input": lambda r: r.insert(
                r.index(record(r, "cleaner-notice")), user("other-input", None, "another task")
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                items = copy.deepcopy(valid)
                mutate(items)
                self.write(relink(items))
                result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
                self.assertFalse(result["lead"]["agent_completed"], label)

    def test_retry_does_not_replace_a_bounded_direct_final(self):
        submitted = native_development_task()
        records = retry_pack_history(submitted["submitted_text"])
        index = next(i for i, item in enumerate(records) if item["id"] == "coder-notice")
        records.insert(index, assistant("direct-final", None, "earlier final"))
        self.write(relink(records))
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"]["final_message_id"], "direct-final")
        self.assertEqual(result["lead"]["answer"], "earlier final")

    def test_retry_allows_any_number_of_unrelated_read_only_polls(self):
        submitted = native_development_task()
        for count in (0, 1, 4):
            with self.subTest(count=count):
                records = retry_pack_history(submitted["submitted_text"])
                binding = next(item for item in records if item["id"] == "binding-call")
                binding["message"]["content"][0]["arguments"]["command"] += (
                    "; hc ls --json | jq -c '[.terminals[]|{address,status,turn}]'"
                )
                binding_output = next(item for item in records if item["id"] == "binding-output")
                terminals = [
                    {"address": f"unrelated-{i}", "status": "idle", "turn": i} for i in range(count)
                ]
                binding_output["message"]["content"][0]["text"] += "\n" + json.dumps(terminals)
                index = next(i for i, item in enumerate(records) if item["id"] == "coder-notice")
                polls = []
                for number in range(count):
                    ident = f"poll-{number}"
                    command = (
                        "hc wait worker1 --timeout 10 --json; helper=/fixture/scripts/handoff.py; "
                        f'python3 "$helper" "$(realpath ../store)" two {PACK} resume | jq -c \'{{phase,current}}\'; '
                        "hc ls --json | jq -c '[.terminals[]|{address,status,turn}]'"
                    )
                    polls.extend(
                        [
                            assistant(
                                ident,
                                None,
                                [
                                    {
                                        "type": "toolCall",
                                        "id": ident + "-call",
                                        "name": "bash",
                                        "arguments": {"command": command},
                                    }
                                ],
                                "toolUse",
                            ),
                            entry(
                                ident + "-output",
                                None,
                                "toolResult",
                                [{"type": "text", "text": "unrelated inventory"}],
                                toolCallId=ident + "-call",
                                toolName="bash",
                                isError=bool(number % 2),
                            ),
                        ]
                    )
                records[index:index] = polls
                # Repeated binding-shaped polls are independent observations,
                # not extra origins or a fixed terminal-count requirement.
                if count:
                    original_call = next(item for item in records if item["id"] == "binding-call")
                    original_result = next(
                        item for item in records if item["id"] == "binding-output"
                    )
                    repeat_call, repeat_result = (
                        copy.deepcopy(original_call),
                        copy.deepcopy(original_result),
                    )
                    repeat_call["id"] = "repeat-binding"
                    repeat_call["message"]["content"][0]["id"] = "repeat-binding-call"
                    repeat_result["id"] = "repeat-binding-output"
                    repeat_result["message"]["toolCallId"] = "repeat-binding-call"
                    index = next(
                        i for i, item in enumerate(records) if item["id"] == "coder-notice"
                    )
                    records[index:index] = [repeat_call, repeat_result]
                self.write(relink(records))
                result, audit = pi_evidence.extract_selected_turns(
                    self.home, [submitted], SELECTION
                )
                self.assertEqual(audit, {"state": "matched"})
                self.assertEqual(result["lead"]["final_message_id"], "final")

    def history_with_pack_init(self, initialized_pack):
        submitted = native_development_task()
        records = two_pack_history(submitted["submitted_text"])
        index = next(i for i, record in enumerate(records) if record["id"] == "init")
        records[index : index + 2] = pack_initialization(initialized_pack)
        return submitted, relink(records)

    def test_two_pack_requires_one_matching_successful_prior_initialization(self):
        submitted, valid = self.history_with_pack_init(PACK)

        def record(records, identifier):
            return next(item for item in records if item["id"] == identifier)

        def change_origin(records, **changes):
            output = record(records, "init-result")["message"]["content"][0]
            output["text"] = json.dumps({**json.loads(output["text"]), **changes})

        def move_after(records, identifier, target):
            item = record(records, identifier)
            records.remove(item)
            records.insert(records.index(record(records, target)) + 1, item)

        cases = {
            "missing origin": lambda r: r.__setitem__(
                slice(None), [x for x in r if x["id"] not in ("init", "init-result")]
            ),
            "failed origin": lambda r: record(r, "init-result")["message"].update(isError=True),
            "unmatched origin": lambda r: record(r, "init-result")["message"].update(
                toolCallId="not-init"
            ),
            "origin result before call": lambda r: move_after(r, "init", "init-result"),
            "origin after binding": lambda r: move_after(r, "init-result", "binding-output"),
            "origin after notification": lambda r: move_after(r, "init-result", "coder-notice"),
            "different origin store": lambda r: change_origin(r, directory="/elsewhere/store"),
            "different baseline": lambda r: change_origin(r, baseline="f" * 40),
            "different branch": lambda r: change_origin(r, branch="other"),
            "mismatched run field": lambda r: change_origin(r, run="f" * 32),
            "origin command extra shell": lambda r: record(r, "init")["message"]["content"][0][
                "arguments"
            ].update(
                command=record(r, "init")["message"]["content"][0]["arguments"]["command"]
                + "; echo forged"
            ),
            "origin worker mismatch": lambda r: record(r, "init")["message"]["content"][0][
                "arguments"
            ].update(
                command=record(r, "init")["message"]["content"][0]["arguments"]["command"].replace(
                    "--coder worker1", "--coder other"
                )
            ),
            "origin other project": lambda r: record(r, "init")["message"]["content"][0][
                "arguments"
            ].update(
                command=record(r, "init")["message"]["content"][0]["arguments"]["command"].replace(
                    "--project /fixture/project", "--project /other/project"
                )
            ),
            "duplicate origin": lambda r: r.insert(
                r.index(record(r, "binding-call")),
                {**copy.deepcopy(record(r, "init")), "id": "init-again"},
            ),
            "ambiguous second init": lambda r: r.insert(
                r.index(record(r, "binding-call")),
                assistant(
                    "other-init",
                    None,
                    [
                        {
                            "type": "toolCall",
                            "id": "other-init-call",
                            "name": "bash",
                            "arguments": {
                                "command": "python3 /fixture/scripts/handoff.py /fixture/store two init --project /other; echo status",
                                "timeout": 20,
                            },
                        }
                    ],
                    "toolUse",
                ),
            ),
            "unresolved second resume": lambda r: r.insert(
                r.index(record(r, "complete-call")),
                assistant(
                    "pending-resume",
                    None,
                    [
                        {
                            "type": "toolCall",
                            "id": "pending-resume-call",
                            "name": "bash",
                            "arguments": {
                                "command": f"python3 /fixture/scripts/handoff.py /fixture/store two {PACK} resume",
                                "timeout": 20,
                            },
                        }
                    ],
                    "toolUse",
                ),
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                records = copy.deepcopy(valid)
                mutate(records)
                self.write(relink(records))
                result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
                self.assertFalse(result["lead"]["agent_completed"], label)

    def test_two_pack_bash_calls_allow_command_alone_but_not_unknown_or_invalid_arguments(self):
        for extra, expected in (
            ({}, True),
            ({"timeout": None}, False),
            ({"timeout": True}, False),
            ({"cwd": "/other"}, False),
        ):
            with self.subTest(extra=extra):
                submitted = native_development_task()
                records = two_pack_history(submitted["submitted_text"])
                for record in records:
                    if record["id"] in ("init", "binding-call", "complete-call"):
                        arguments = record["message"]["content"][0]["arguments"]
                        del arguments["timeout"]
                        arguments.update(extra)
                self.write(records)
                result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
                self.assertEqual(result["lead"]["agent_completed"], expected)

    def test_two_pack_initialization_accepts_the_recorded_local_variable_form(self):
        submitted, records = self.history_with_pack_init(PACK)
        init = next(item for item in records if item["id"] == "init")
        init["message"]["content"][0]["arguments"]["command"] = (
            "set -e; root=/fixture; helper=/fixture/scripts/handoff.py; "
            'python3 "$helper" "$root/store" two init --project "$root/project" '
            '--card "$root/card" --checks "$root/checks" --coder worker1 --cleaner worker2'
        )
        self.write(records)
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"]["final_message_id"], "final")

    def test_two_pack_rejects_receipts_for_another_run_than_the_one_initialized(self):
        # Same store, branch, baseline and workers. Only the run differs.
        submitted, records = self.history_with_pack_init("e" * 32)
        self.write(records)
        result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertFalse(
            result["lead"]["agent_completed"], "an inspected run is not the initialized run"
        )

    def test_two_pack_extra_visible_terminal_does_not_change_the_workers(self):
        submitted, records = self.history_with_pack_init(PACK)
        for record in records:
            if record["id"] in ("binding-output", "complete-output"):
                record["message"]["content"][0]["text"] += (
                    '\n{"address":"unrelated","turn":1,"status":"idle"}'
                )
        self.write(records)
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"]["final_message_id"], "final")

    def test_two_pack_accepts_plain_resume_structured_result(self):
        submitted, records = self.history_with_pack_init(PACK)
        by_id = {record["id"]: record for record in records}
        card = {
            "version": 1,
            "body": "Change the fixture value",
            "checks": {"coder": [], "cleaner": []},
            "created_at": STAMP,
            "approval": "initial task card supplied at init",
            "approved_at": STAMP,
        }
        for phase in ("binding", "complete"):
            call = by_id[f"{phase}-call"]["message"]["content"][0]
            call["arguments"]["command"] = (
                f"python3 /fixture/scripts/handoff.py /fixture/store two {PACK} resume"
            )
            output = by_id[f"{phase}-output"]["message"]["content"][0]
            snapshot = json.loads(output["text"].splitlines()[1])
            completed = phase == "complete"
            projected = snapshot.pop(
                "assignments",
                [{"id": f"{PACK}/coder/1", "role": "coder", "target": "worker1", "rejections": []}],
            )
            assignments = {}
            for item in projected:
                role = item["role"]
                assignments[f"{role}:1"] = {
                    **item,
                    "round": 1,
                    "created_at": STAMP,
                    "card_version": 1,
                    "source_receipt": None if role == "coder" else 1,
                    "input_commit": ("b" if role == "coder" else "c") * 40,
                    "attempts": [],
                    "delivery": {"status": "delivered"},
                }
            snapshot.pop("round_count", None)
            output["text"] = json.dumps(
                {
                    **snapshot,
                    "version": 1,
                    "pack": "two",
                    "pack_version": 1,
                    "project": "/fixture/project",
                    "project_id": "fixture-project",
                    "rounds": 1,
                    "branch": "fixture",
                    "baseline": "b" * 40,
                    "assignments": assignments,
                    "phase": "completed" if completed else "active",
                    "card_version": 1,
                    "card_versions": [card],
                    "card": card,
                    "checks": card["checks"],
                    "created_at": STAMP,
                    "updated_at": STAMP,
                }
            )
        self.write(records)
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"]["final_message_id"], "final")

        # Full snapshots must not gain a less conservative path than projections.
        def change_snapshot(items, identifier, mutate):
            output = next(item for item in items if item["id"] == identifier)["message"]["content"][
                0
            ]
            value = json.loads(output["text"])
            mutate(value)
            output["text"] = json.dumps(value)

        cases = {
            "malformed current": ("binding-output", lambda s: s.update(current=[])),
            "other project": ("binding-output", lambda s: s.update(project="/other")),
            "other version": ("binding-output", lambda s: s.update(version=2)),
            "wrong assignment target": (
                "binding-output",
                lambda s: s["assignments"]["coder:1"].update(target="other"),
            ),
            "wrong baseline": ("complete-output", lambda s: s.update(baseline="f" * 40)),
            "other assignment input": (
                "complete-output",
                lambda s: s["assignments"]["cleaner:1"].update(input_commit="f" * 40),
            ),
            "other source receipt": (
                "complete-output",
                lambda s: s["assignments"]["cleaner:1"].update(source_receipt=17),
            ),
            "missing accepted receipt": (
                "complete-output",
                lambda s: s["assignments"]["cleaner:1"].update(receipt=None),
            ),
        }
        for label, (identifier, mutate) in cases.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(records)
                change_snapshot(changed, identifier, mutate)
                self.write(changed)
                result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
                self.assertFalse(result["lead"]["agent_completed"], label)

    def test_two_pack_does_not_cross_an_independent_bridge_input(self):
        submitted = native_development_task()
        entries = two_pack_history(submitted["submitted_text"])
        index = next(i for i, e in enumerate(entries) if e["id"] == "cleaner-notice")
        entries[index:index] = [
            user("independent", None, "independent task"),
            assistant("independent-answer", None, "independent answer"),
        ]
        self.write(relink(entries))
        other = task("other", "independent task")
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted, other], SELECTION)
        self.assertFalse(result["lead"]["agent_completed"])
        self.assertEqual(result["other"]["final_message_id"], "independent-answer")
        self.assertEqual(result["other"]["answer"], "independent answer")
        self.assertEqual(audit["state"], "pending")

    def test_two_pack_requires_native_development_and_unbounded_complete_history(self):
        submitted = native_development_task()
        entries = two_pack_history(submitted["submitted_text"])
        for label, tasks, records, tail in (
            ("ordinary task", [task("lead", submitted["submitted_text"])], entries, b""),
            (
                "isolated task",
                [
                    {
                        **submitted,
                        "submitted_text": submitted["submitted_text"].replace(
                            "Use your normal tools, extensions and skills for this task.",
                            "Use scoped_edit/scoped_write and run_check for changes and checks.",
                        ),
                    }
                ],
                two_pack_history(
                    submitted["submitted_text"].replace(
                        "Use your normal tools, extensions and skills for this task.",
                        "Use scoped_edit/scoped_write and run_check for changes and checks.",
                    )
                ),
                b"",
            ),
            ("partial final", [submitted], entries, b'{"type":'),
            (
                "aborted original",
                [submitted],
                relink(
                    copy.deepcopy(entries[:8])
                    + [assistant("abort", None, [], stop="aborted")]
                    + copy.deepcopy(entries[8:])
                ),
                b"",
            ),
        ):
            with self.subTest(label=label):
                self.write(records, tail)
                result, _ = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
                self.assertFalse(result["lead"]["agent_completed"])

    def test_two_pack_does_not_override_restored_lost_turn(self):
        submitted = {
            **native_development_task(),
            "recovery": {"state": "restored", "thread_id": THREAD, "agent_turn_id": "original"},
        }
        self.write(two_pack_history(submitted["submitted_text"]))
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertTrue(result["lead"]["agent_lost"])
        self.assertFalse(result["lead"]["agent_completed"])

    def test_two_pack_does_not_swallow_a_registered_notification_shaped_input(self):
        submitted = native_development_task()
        entries = two_pack_history(submitted["submitted_text"])
        notice = next(e for e in entries if e["id"] == "coder-notice")["message"]["content"]
        self.write(entries)
        result, _ = pi_evidence.extract_selected_turns(
            self.home, [submitted, task("independent", notice)], SELECTION
        )
        self.assertFalse(result["lead"]["agent_completed"])
        self.assertEqual(result["independent"]["agent_turn_id"], "coder-notice")

    def test_two_pack_rejects_unsupported_or_uncorroborated_inputs(self):
        submitted = native_development_task()
        original = two_pack_history(submitted["submitted_text"])

        def by_id(records, ident):
            return next(e for e in records if e["id"] == ident)

        def mutate_content(records, ident, mutate):
            message = by_id(records, ident)["message"]
            objs = []
            decoder = json.JSONDecoder()
            data = message["content"][0]["text"]
            while data.strip():
                value, end = decoder.raw_decode(data.lstrip())
                objs.append(value)
                data = data.lstrip()[end:]
            mutate(objs)
            message["content"][0]["text"] = "\n".join(map(json.dumps, objs))

        cases = {
            "question": lambda r: by_id(r, "final")["message"]["content"][0].update(
                text=json.dumps({"task_id": "lead", "kind": "question", "answer": {}})
            ),
            "earlier question": lambda r: r.insert(
                next(i for i, e in enumerate(r) if e["id"] == "complete-call"),
                assistant(
                    "question",
                    None,
                    json.dumps({"task_id": "lead", "kind": "question", "answer": {}}),
                ),
            ),
            "wrong task": lambda r: by_id(r, "final")["message"]["content"][0].update(
                text=json.dumps({"task_id": "other", "kind": "result", "answer": {}})
            ),
            "unknown notice": lambda r: by_id(r, "coder-notice")["message"].update(
                content='<hc from="worker1">unrelated</hc>'
            ),
            "mixed pack": lambda r: by_id(r, "cleaner-notice")["message"].update(
                content=by_id(r, "cleaner-notice")["message"]["content"].replace(PACK, "f" * 32)
            ),
            "fake shell output": lambda r: by_id(r, "binding-call")["message"]["content"][0][
                "arguments"
            ].update(command="printf arbitrary"),
            "fake script": lambda r: by_id(r, "binding-call")["message"]["content"][0][
                "arguments"
            ].update(
                command=by_id(r, "binding-call")["message"]["content"][0]["arguments"][
                    "command"
                ].replace("/fixture/scripts/handoff.py", "/fixture/scripts/other.py")
            ),
            "non-tooluse call": lambda r: by_id(r, "complete-call")["message"].update(
                stopReason="stop"
            ),
            "different store": lambda r: by_id(r, "complete-call")["message"]["content"][0][
                "arguments"
            ].update(
                command=by_id(r, "complete-call")["message"]["content"][0]["arguments"][
                    "command"
                ].replace("/fixture/store", "/other/store")
            ),
            "unmatched tool call": lambda r: by_id(r, "complete-output")["message"].update(
                toolCallId="unrelated"
            ),
            "wrong tool": lambda r: by_id(r, "complete-output")["message"].update(toolName="read"),
            "error tool": lambda r: by_id(r, "complete-output")["message"].update(isError=True),
            "arbitrary output prefix": lambda r: by_id(r, "complete-output")["message"]["content"][
                0
            ].update(
                text="garbage\n" + by_id(r, "complete-output")["message"]["content"][0]["text"]
            ),
            "unaccepted receipt": lambda r: mutate_content(
                r, "complete-output", lambda o: o[1]["assignments"][1].update(receipt=None)
            ),
            "wrong receipt worker": lambda r: mutate_content(
                r,
                "complete-output",
                lambda o: o[1]["assignments"][0]["receipt"].update(worker="other"),
            ),
            "duplicate assignment": lambda r: mutate_content(
                r,
                "complete-output",
                lambda o: o[1]["assignments"].append(copy.deepcopy(o[1]["assignments"][0])),
            ),
            "conflicting baseline": lambda r: mutate_content(
                r, "complete-output", lambda o: o[1].update(baseline="f" * 40)
            ),
            "rejected only": lambda r: mutate_content(
                r,
                "complete-output",
                lambda o: o[1]["assignments"][1].update(
                    receipt=None, rejections=[{"id": 2, "status": "pass", "worker": "worker2"}]
                ),
            ),
            "duplicate receipt": lambda r: mutate_content(
                r, "complete-output", lambda o: o[1]["assignments"][1]["receipt"].update(id=1)
            ),
            "rejected receipt id reused": lambda r: mutate_content(
                r, "complete-output", lambda o: o[1]["assignments"][1]["rejections"][0].update(id=1)
            ),
            "unbound pack": lambda r: mutate_content(
                r, "binding-output", lambda o: o[1].update(id="f" * 32)
            ),
            "duplicate notice": lambda r: r.insert(
                -1, {**copy.deepcopy(by_id(r, "cleaner-notice")), "id": "duplicate-notice"}
            ),
            "binding after notice": lambda r: r.insert(
                next(i for i, e in enumerate(r) if e["id"] == "coder-notice") + 1,
                r.pop(next(i for i, e in enumerate(r) if e["id"] == "binding-output")),
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                entries = copy.deepcopy(original)
                mutate(entries)
                self.write(relink(entries))
                result, _ = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
                self.assertFalse(result["lead"]["agent_completed"], label)

    def test_two_pack_never_replaces_a_bounded_direct_final(self):
        submitted = native_development_task()
        entries = two_pack_history(submitted["submitted_text"])
        entries.insert(
            next(i for i, e in enumerate(entries) if e["id"] == "coder-notice"),
            assistant("bounded", None, "earlier final"),
        )
        self.write(relink(entries))
        result, audit = pi_evidence.extract_selected_turns(self.home, [submitted], SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["lead"]["final_message_id"], "bounded")
        self.assertEqual(result["lead"]["answer"], "earlier final")

    def test_aborted_turn_is_correlated_and_settled_without_a_successful_answer(self):
        self.write(
            selected_prefix()
            + [user("u", "level", "prompt"), assistant("a", "u", [], stop="aborted")]
        )
        result, audit = pi_evidence.extract_selected_turns(
            self.home, [task("one", "prompt")], SELECTION
        )
        self.assertEqual(audit, {"state": "matched"})
        self.assertTrue(result["one"]["agent_aborted"])
        self.assertFalse(result["one"]["agent_completed"])
        self.assertIsNone(result["one"]["answer"])
        self.assertEqual(result["one"]["aborted_message_id"], "a")

    def test_selected_missing_session_is_pending_and_recovers(self):
        tasks = [task("one", "prompt")]
        result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "pending", "reason": "native_session_missing"})

        self.path.touch()
        result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "pending", "reason": "native_session_incomplete"})
        with patch.object(Path, "read_bytes", side_effect=OSError("private path and content")):
            result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "pending", "reason": "native_session_unavailable"})

        self.write(
            selected_prefix() + [user("u", "level", "prompt"), assistant("a", "u", "answer")]
        )
        result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["one"]["answer"], "answer")

    def test_selected_malformed_complete_record_fails_without_evidence(self):
        entries = selected_prefix() + [
            user("u", "level", "private prompt"),
            assistant("a", "u", "private answer"),
        ]
        bad_lines = (
            b"{private invalid json}\n",
            b"null\n",
            b"[]\n",
            b'{"type":"message","type":"message"}\n',
            b'{"type":"message","id":NaN}\n',
            b"\xff\n",
            json.dumps(
                {"type": "message", "id": "x", "parentId": "a", "timestamp": STAMP, "message": {}}
            ).encode()
            + b"\n",
        )
        for line in bad_lines:
            with self.subTest(line=line):
                self.write(entries, line)
                result, audit = pi_evidence.extract_selected_turns(
                    self.home, [task("one", "private prompt")], SELECTION
                )
                self.assertEqual(result, {})
                self.assertEqual(audit, {"state": "failed", "reason": "native_record_malformed"})
        self.write(entries)
        result, audit = pi_evidence.extract_selected_turns(
            self.home, [task("one", "private prompt")], SELECTION
        )
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["one"]["answer"], "private answer")

    def test_selected_excessively_nested_complete_record_fails_safely(self):
        entries = selected_prefix() + [user("u", "level", "prompt"), assistant("a", "u", "answer")]
        self.write(entries, b"[" * 2000 + b"]" * 2000 + b"\n")
        result, audit = pi_evidence.extract_selected_turns(
            self.home, [task("one", "prompt")], SELECTION
        )
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "failed", "reason": "native_record_malformed"})

    def test_selected_unsupported_version_and_structures_are_definite_failures(self):
        base = selected_prefix() + [user("u", "level", "prompt"), assistant("a", "u", "answer")]
        version = copy.deepcopy(base)
        version[0]["version"] = 4
        fork = copy.deepcopy(base)
        fork[0]["parentSession"] = "/private/session"
        cases = [
            (version, "native_version_unsupported"),
            (fork, "native_structure_unsupported"),
            (
                base + [{"type": "compaction", "id": "x", "parentId": "a", "timestamp": STAMP}],
                "native_structure_unsupported",
            ),
            (base + [entry("x", "a", "future-role", "private")], "native_structure_unsupported"),
            (
                selected_prefix() + [user("u", "level", [{"type": "image"}])],
                "native_structure_unsupported",
            ),
            (
                selected_prefix()
                + [user("u", "level", "prompt"), assistant("a", "u", [{"type": "future"}])],
                "native_structure_unsupported",
            ),
            (
                selected_prefix()
                + [user("u", "level", "prompt"), assistant("a", "u", "answer", "future-stop")],
                "native_structure_unsupported",
            ),
        ]
        for entries, reason in cases:
            with self.subTest(reason=reason, entries=entries):
                self.write(entries)
                result, audit = pi_evidence.extract_selected_turns(
                    self.home, [task("one", "prompt")], SELECTION
                )
                self.assertEqual(result, {})
                self.assertEqual(audit, {"state": "failed", "reason": reason})

    def test_selected_ambiguous_history_fails_without_completed_evidence(self):
        base = selected_prefix() + [user("u", "level", "prompt"), assistant("a", "u", "answer")]
        for trailing in (user("other", "u", "branch"), user("a", "a", "duplicate id")):
            with self.subTest(trailing=trailing):
                self.write(base + [trailing])
                result, audit = pi_evidence.extract_selected_turns(
                    self.home, [task("one", "prompt")], SELECTION
                )
                self.assertEqual(result, {})
                self.assertEqual(audit, {"state": "failed", "reason": "native_history_ambiguous"})

    def test_selected_multiple_or_symlinked_sessions_are_rejected(self):
        self.write(
            selected_prefix() + [user("u", "level", "prompt"), assistant("a", "u", "answer")]
        )
        other = self.path.parent / "another.jsonl"
        other.write_text("{}\n")
        result, audit = pi_evidence.extract_selected_turns(
            self.home, [task("one", "prompt")], SELECTION
        )
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "failed", "reason": "native_session_ambiguous"})

        other.unlink()
        self.path.unlink()
        external = Path(self.temp.name) / "private.jsonl"
        external.write_text("{}\n")
        self.path.symlink_to(external)
        result, audit = pi_evidence.extract_selected_turns(
            self.home, [task("one", "prompt")], SELECTION
        )
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "failed", "reason": "native_session_unsupported"})

    def test_selected_invalid_input_is_a_safe_failure(self):
        self.write(
            selected_prefix() + [user("u", "level", "prompt"), assistant("a", "u", "answer")]
        )
        cases = [
            (None, SELECTION),
            ([{}], SELECTION),
            ([task("one", None)], SELECTION),
            ([task("", "prompt")], SELECTION),
            ([task("one", "prompt")], None),
            ([task("one", "prompt")], {}),
            (
                [task("one", "prompt")],
                {"session_id": THREAD, "model": "private", "thinking": "high"},
            ),
        ]
        for tasks, selection in cases:
            with self.subTest(tasks=tasks, selection=selection):
                result, audit = pi_evidence.extract_selected_turns(self.home, tasks, selection)
                self.assertEqual(result, {})
                self.assertEqual(audit, {"state": "failed", "reason": "native_input_invalid"})

    def test_selected_invalid_home_is_a_safe_failure(self):
        result, audit = pi_evidence.extract_selected_turns(None, [task("one", "prompt")], SELECTION)
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "failed", "reason": "native_input_invalid"})

    def test_selected_ambiguous_inputs_fail_but_keep_unambiguous_legacy_matches(self):
        entries = selected_prefix() + [
            user("u1", "level", "repeated"),
            assistant("a1", "u1", "old"),
            user("u2", "a1", "unique"),
            assistant("a2", "u2", "safe"),
            user("u3", "a2", "repeated "),
            assistant("a3", "u3", "new"),
        ]
        self.write(entries)
        cases = [
            [task("ambiguous", "repeated"), task("safe", "unique")],
            [task("duplicate", "repeated"), task("duplicate", "other"), task("safe", "unique")],
            [task("one", "repeated"), task("two", "repeated\n"), task("safe", "unique")],
        ]
        for tasks in cases:
            with self.subTest(tasks=tasks):
                result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
                self.assertEqual(audit, {"state": "failed", "reason": "native_input_ambiguous"})
                self.assertEqual(result, pi_evidence.extract_turns(self.home, tasks))
                self.assertEqual(set(result), {"safe"})
                self.assertEqual(result["safe"]["answer"], "safe")

    def test_selected_ambiguous_submissions_fail_even_before_a_session_exists(self):
        for tasks in (
            [task("one", "same"), task("two", "same\n")],
            [task("same-id", "one"), task("same-id", "two")],
            [task("empty", " \n")],
        ):
            with self.subTest(tasks=tasks):
                result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
                self.assertEqual(result, {})
                self.assertEqual(audit, {"state": "failed", "reason": "native_input_ambiguous"})

    def test_selected_no_match_and_incomplete_turn_remain_pending_until_final(self):
        tasks = [task("one", "prompt")]
        entries = selected_prefix()
        self.write(entries)
        result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "pending", "reason": "native_no_match"})

        self.write(
            entries
            + [
                user("other", "level", "prompt "),
                assistant("other-answer", "other", "not an exact match"),
            ]
        )
        result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "pending", "reason": "native_no_match"})

        entries += [user("u", "level", "prompt"), assistant("a1", "u", "unfinished", "pending")]
        self.write(entries)
        result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertFalse(result["one"]["agent_completed"])
        self.assertEqual(audit, {"state": "pending", "reason": "native_turn_incomplete"})

        self.write(entries + [assistant("a2", "a1", "final")])
        result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["one"]["answer"], "final")

    def test_selected_trailing_write_is_pending_until_newline_and_preserves_bounded_turn(self):
        tasks = [task("one", "first"), task("two", "second")]
        entries = selected_prefix() + [
            user("u1", "level", "first"),
            assistant("a1", "u1", "old"),
            user("u2", "a1", "second"),
            assistant("a2", "u2", "superseded"),
        ]
        final = json.dumps(assistant("a3", "a2", "new")).encode("utf-8")
        for written in (15, len(final)):
            with self.subTest(written=written):
                self.write(entries, final[:written])
                result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
                self.assertEqual(audit, {"state": "pending", "reason": "native_trailing_write"})
                self.assertEqual(result["one"]["answer"], "old")
                self.assertFalse(result["two"]["agent_completed"])

                with self.path.open("ab") as stream:
                    stream.write(final[written:] + b"\n")
                result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
                self.assertEqual(audit, {"state": "matched"})
                self.assertEqual(result["two"]["answer"], "new")

        self.path.write_bytes(json.dumps(header()).encode("utf-8"))
        result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "pending", "reason": "native_trailing_write"})

    def test_selected_header_waits_for_selection_events(self):
        tasks = [task("one", "prompt")]
        self.write([header()])
        result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "pending", "reason": "native_selection_pending"})

        self.write(
            selected_prefix() + [user("u", "level", "prompt"), assistant("a", "u", "answer")]
        )
        result, audit = pi_evidence.extract_selected_turns(self.home, tasks, SELECTION)
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["one"]["answer"], "answer")

    def test_sequential_turns_use_native_ids_and_question_json_is_unmodified(self):
        question = json.dumps(
            {"task_id": "two", "kind": "question", "answer": {"question": "English or 中文?"}},
            ensure_ascii=False,
        )
        entries = [
            header(),
            {
                "type": "model_change",
                "id": "m1",
                "parentId": None,
                "timestamp": STAMP,
                "provider": "fixture-provider",
                "modelId": "fixture-model",
            },
            entry("system", "m1", "system", ""),
            user("u1", "system", "first exact prompt"),
            assistant("a1", "u1", "first answer"),
            user(
                "u2",
                "a1",
                [{"type": "text", "text": "second "}, {"type": "text", "text": "prompt"}],
            ),
            assistant(
                "a2",
                "u2",
                [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": question[:20]},
                    {"type": "text", "text": question[20:]},
                ],
            ),
        ]
        result = self.evidence(
            entries, [task("one", "first exact prompt"), task("two", "second prompt")]
        )
        self.assertEqual(
            result,
            {
                "one": {
                    "thread_id": THREAD,
                    "agent_turn_id": "u1",
                    "answer": "first answer",
                    "agent_completed": True,
                    "final_message_id": "a1",
                    "turn_id_source": "pi_user_message_id",
                },
                "two": {
                    "thread_id": THREAD,
                    "agent_turn_id": "u2",
                    "answer": question,
                    "agent_completed": True,
                    "final_message_id": "a2",
                    "turn_id_source": "pi_user_message_id",
                },
            },
        )

    def test_tool_call_and_result_precede_final_answer(self):
        call = {
            "type": "toolCall",
            "id": "call-1",
            "name": "read",
            "arguments": {"path": "/fixture"},
        }
        entries = [
            header(),
            user("u", None, "inspect"),
            assistant("a1", "u", [{"type": "text", "text": "reading"}, call], "toolUse"),
            entry(
                "tool",
                "a1",
                "toolResult",
                [{"type": "text", "text": "fixture"}],
                toolCallId="call-1",
                toolName="read",
                isError=False,
            ),
            assistant("a2", "tool", "final fixture answer"),
        ]
        result = self.evidence(entries, [task("one", "inspect")])["one"]
        self.assertEqual(result["agent_turn_id"], "u")
        self.assertEqual(result["final_message_id"], "a2")
        self.assertEqual(result["answer"], "final fixture answer")
        self.assertTrue(result["agent_completed"])

    def test_state_only_entries_preserve_linear_evidence(self):
        entries = [
            header(),
            {
                "type": "thinking_level_change",
                "id": "level",
                "parentId": None,
                "timestamp": STAMP,
                "thinkingLevel": "high",
            },
            user("u", "level", "prompt"),
            assistant("a", "u", "answer"),
            {
                "type": "usage",
                "id": "usage",
                "parentId": "a",
                "timestamp": STAMP,
                "kind": "cache_warm",
                "provider": "provider",
                "model": "model",
                "usage": {},
            },
            {
                "type": "session_info",
                "id": "info",
                "parentId": "usage",
                "timestamp": STAMP,
                "name": "fixture",
            },
            {
                "type": "custom",
                "id": "state",
                "parentId": "info",
                "timestamp": STAMP,
                "customType": "fixture-state",
            },
            {
                "type": "label",
                "id": "label",
                "parentId": "state",
                "timestamp": STAMP,
                "targetId": "a",
                "label": "fixture",
            },
        ]
        result = self.evidence(entries, [task("t", "prompt")])["t"]
        self.assertTrue(result["agent_completed"])
        self.assertEqual(result["answer"], "answer")

    def test_selected_session_preserves_selection_and_dispatch_failure_reasons(self):
        model = {
            "type": "model_change",
            "id": "m",
            "parentId": None,
            "timestamp": STAMP,
            "provider": "fixture-provider",
            "modelId": "selected-model",
        }
        thinking = {
            "type": "thinking_level_change",
            "id": "t",
            "parentId": "m",
            "timestamp": STAMP,
            "thinkingLevel": "high",
        }
        self.write(
            [header(), model, thinking, user("u", "t", "prompt"), assistant("a", "u", "answer")]
        )
        result, audit = pi_evidence.extract_selected_turns(
            self.home,
            [task("one", "prompt")],
            {"session_id": THREAD, "model": "fixture-provider/selected-model", "thinking": "high"},
        )
        self.assertEqual(result, {})
        self.assertEqual(audit, {"state": "failed", "reason": "native_dispatch_mismatch"})

        wrong_session = header()
        wrong_session["id"] = "other-session"
        wrong_model = selected_prefix()
        wrong_model[1]["modelId"] = "other-model"
        wrong_thinking = selected_prefix()
        wrong_thinking[2]["thinkingLevel"] = "low"
        cases = [
            ([wrong_session], "native_session_mismatch"),
            (wrong_model, "native_selection_mismatch"),
            (wrong_thinking, "native_selection_mismatch"),
            ([header(), user("u", None, "prompt")], "native_selection_unavailable"),
        ]
        for entries, reason in cases:
            with self.subTest(reason=reason):
                self.write(entries)
                result, audit = pi_evidence.extract_selected_turns(
                    self.home, [task("one", "prompt")], SELECTION
                )
                self.assertEqual(result, {})
                self.assertEqual(audit, {"state": "failed", "reason": reason})

    def test_selected_session_correlates_editor_trimmed_input(self):
        model = {
            "type": "model_change",
            "id": "m",
            "parentId": None,
            "timestamp": STAMP,
            "provider": "fixture-provider",
            "modelId": "fixture-model",
        }
        thinking = {
            "type": "thinking_level_change",
            "id": "level",
            "parentId": "m",
            "timestamp": STAMP,
            "thinkingLevel": "high",
        }
        self.write(
            [header(), model, thinking, user("u", "level", "prompt"), assistant("a", "u", "answer")]
        )
        result, audit = pi_evidence.extract_selected_turns(
            self.home,
            [task("one", "prompt\n")],
            {"session_id": THREAD, "model": "fixture-provider/fixture-model", "thinking": "high"},
        )
        self.assertEqual(audit, {"state": "matched"})
        self.assertEqual(result["one"]["agent_turn_id"], "u")
        self.assertEqual(result["one"]["answer"], "answer")

    def test_exact_user_match_only_and_other_role_echoes_do_not_match(self):
        entries = [
            header(),
            entry("s", None, "system", "exact"),
            assistant("a0", "s", "exact"),
            user("u1", "a0", "exactx"),
            assistant("a1", "u1", "exact"),
            user("u2", "a1", "x\ny"),
        ]
        tasks = [
            task("wrong", "exact"),
            task("close", "exactx"),
            task("injected_newline", "xy"),
            task("right", "x\ny"),
        ]
        result = self.evidence(entries, tasks)
        self.assertEqual(set(result), {"close", "right"})
        self.assertEqual(result["right"]["agent_turn_id"], "u2")
        self.assertFalse(result["right"]["agent_completed"])

    def test_editor_submission_trims_final_newline_without_changing_task(self):
        entries = [header(), user("u", None, "prompt"), assistant("a", "u", "answer")]
        submitted = task("t", "prompt\n")
        result = self.evidence(entries, [submitted])
        self.assertEqual(result["t"]["agent_turn_id"], "u")
        self.assertEqual(result["t"]["answer"], "answer")
        self.assertEqual(submitted["submitted_text"], "prompt\n")

    def test_native_messages_colliding_after_editor_trim_are_ambiguous(self):
        entries = [
            header(),
            user("u1", None, "repeated"),
            assistant("a1", "u1", "old"),
            user("u2", "a1", "unique"),
            assistant("a2", "u2", "good"),
            user("u3", "a2", "repeated "),
            assistant("a3", "u3", "new"),
        ]
        result = self.evidence(entries, [task("ambiguous", "repeated\n"), task("safe", "unique")])
        self.assertEqual(set(result), {"safe"})
        self.assertEqual(result["safe"]["answer"], "good")

    def test_distinct_submitted_inputs_colliding_after_editor_trim_are_ambiguous(self):
        entries = [header(), user("u", None, "prompt"), assistant("a", "u", "answer")]
        result = self.evidence(entries, [task("one", "prompt"), task("two", "prompt\n")])
        self.assertEqual(result, {})

    def test_editor_trim_uses_javascript_whitespace_and_native_text_stays_exact(self):
        entries = [
            header(),
            user("u1", None, "one \ntwo"),
            assistant("a1", "u1", "answer"),
            user("u2", "a1", "prompt "),
            assistant("a2", "u2", "other"),
        ]
        tasks = [
            task("trimmed", "\ufeff\u00a0one \ntwo\t\u2029"),
            task("not_js_whitespace", "\u0085one \ntwo"),
            task("native_not_trimmed", "prompt"),
        ]
        result = self.evidence(entries, tasks)
        self.assertEqual(set(result), {"trimmed"})
        self.assertEqual(result["trimmed"]["agent_turn_id"], "u1")

    def test_duplicate_native_text_only_invalidates_that_task(self):
        entries = [
            header(),
            user("u1", None, "repeated"),
            assistant("a1", "u1", "old"),
            user("u2", "a1", "unique"),
            assistant("a2", "u2", "good"),
            user("u3", "a2", "repeated"),
            assistant("a3", "u3", "new"),
        ]
        result = self.evidence(entries, [task("ambiguous", "repeated"), task("safe", "unique")])
        self.assertEqual(set(result), {"safe"})
        self.assertEqual(result["safe"]["agent_turn_id"], "u2")
        self.assertEqual(result["safe"]["answer"], "good")

    def test_duplicate_task_ids_or_submitted_texts_cannot_map_two_tasks_to_one_turn(self):
        entries = [
            header(),
            user("u1", None, "same"),
            assistant("a1", "u1", "reply"),
            user("u2", "a1", "other"),
            assistant("a2", "u2", "reply 2"),
        ]
        for tasks in (
            [task("a", "same"), task("b", "same")],
            [task("a", "same"), task("a", "other")],
        ):
            with self.subTest(tasks=tasks):
                self.assertEqual(self.evidence(entries, tasks), {})

    def test_unfinished_reasons_or_content_never_complete(self):
        call = {"type": "toolCall", "id": "call", "name": "read", "arguments": {}}
        scenarios = [
            (reason, [{"type": "text", "text": "not final"}], {})
            for reason in ("pending", "length", "toolUse", "error", "aborted", "deferred")
        ]
        scenarios += [
            ("stop", [call], {}),
            ("stop", [{"type": "text", "text": "not final"}, call], {}),
            ("stop", [{"type": "thinking", "thinking": "reasoning"}], {}),
            ("stop", [], {}),
            ("stop", [{"type": "text", "text": "not final"}], {"endTurn": False}),
        ]
        for reason, content, extra in scenarios:
            with self.subTest(reason=reason, content=content, extra=extra):
                entries = [
                    header(),
                    user("u", None, "prompt"),
                    assistant("a", "u", content, reason, **extra),
                ]
                result = self.evidence(entries, [task("t", "prompt")])["t"]
                self.assertFalse(result["agent_completed"])
                self.assertIsNone(result["answer"])
                self.assertNotIn("final_message_id", result)

    def test_last_assistant_or_following_tool_result_controls_completion(self):
        for trailing in (
            assistant("a2", "a1", "error body", "error"),
            entry(
                "tool",
                "a1",
                "toolResult",
                [{"type": "text", "text": "output"}],
                toolCallId="call",
                toolName="read",
                isError=False,
            ),
        ):
            with self.subTest(trailing=trailing):
                entries = [
                    header(),
                    user("u", None, "prompt"),
                    assistant("a1", "u", "earlier text"),
                    trailing,
                ]
                result = self.evidence(entries, [task("t", "prompt")])["t"]
                self.assertFalse(result["agent_completed"])
                self.assertIsNone(result["answer"])

    def test_partial_tail_keeps_bounded_prior_turn_but_never_completes_current_turn(self):
        entries = [
            header(),
            user("u1", None, "one"),
            assistant("a1", "u1", "answer one"),
            user("u2", "a1", "two"),
            assistant("a2", "u2", "old-looking answer"),
        ]
        for tail in (
            b'{"type":"message","id":"a3",',
            b"\xe4\xb8",
            json.dumps(assistant("a3", "a2", "valid without newline")).encode(),
        ):
            with self.subTest(tail=tail):
                result = self.evidence(entries, [task("one", "one"), task("two", "two")], tail)
                self.assertTrue(result["one"]["agent_completed"])
                self.assertEqual(result["one"]["answer"], "answer one")
                self.assertFalse(result["two"]["agent_completed"])
                self.assertIsNone(result["two"]["answer"])

    def test_pending_current_turn_does_not_erase_completed_prior_turn(self):
        entries = [
            header(),
            user("u1", None, "one"),
            assistant("a1", "u1", "answer one"),
            user("u2", "a1", "two"),
            assistant("a2", "u2", "waiting", "pending"),
        ]
        result = self.evidence(entries, [task("one", "one"), task("two", "two")])
        self.assertTrue(result["one"]["agent_completed"])
        self.assertFalse(result["two"]["agent_completed"])

    def test_missing_or_wrong_native_metadata_fails_closed(self):
        original = [header(), user("u", None, "prompt"), assistant("a", "u", "answer")]
        mutations = [
            lambda e: e[0].pop("version"),
            lambda e: e[0].update(version=2),
            lambda e: e[0].update(version=True),
            lambda e: e[0].pop("id"),
            lambda e: e[0].pop("cwd"),
            lambda e: e[0].pop("timestamp"),
            lambda e: e[1].pop("id"),
            lambda e: e[1].pop("parentId"),
            lambda e: e[1].pop("timestamp"),
            lambda e: e[1]["message"].pop("timestamp"),
            lambda e: e[1]["message"].update(timestamp=True),
            lambda e: e[2]["message"].pop("stopReason"),
            lambda e: e[2]["message"].update(stopReason=["stop"]),
            lambda e: e[2]["message"].pop("api"),
            lambda e: e[2]["message"].pop("usage"),
            lambda e: e[2]["message"].update(content="not a block array"),
        ]
        for mutate in mutations:
            entries = copy.deepcopy(original)
            mutate(entries)
            with self.subTest(entries=entries):
                self.assertEqual(self.evidence(entries, [task("t", "prompt")]), {})

    def test_malformed_complete_lines_and_bad_json_shapes_fail_closed(self):
        prefix = [header(), user("u", None, "prompt")]
        for bad in (
            b"{bad json}\n",
            b"null\n",
            b"[]\n",
            b'{"type":"message","type":"message"}\n',
            b'{"type":"message","id":NaN}\n',
        ):
            with self.subTest(bad=bad):
                self.write(prefix, bad + json.dumps(assistant("a", "u", "answer")).encode() + b"\n")
                self.assertEqual(pi_evidence.extract_turns(self.home, [task("t", "prompt")]), {})

    def test_branches_forks_context_changes_duplicate_ids_and_unknown_formats_fail_closed(self):
        base = [header(), user("u", None, "prompt"), assistant("a", "u", "answer")]
        cases = [
            base + [user("u2", "u", "branch")],
            base + [user("u2", None, "another root")],
            base + [user("a", "a", "duplicate")],
            base + [header()],
        ]
        for kind in (
            "compaction",
            "context_edit",
            "branch_summary",
            "custom_message",
            "future_type",
        ):
            cases.append(base + [{"type": kind, "id": "x", "parentId": "a", "timestamp": STAMP}])
        forked = copy.deepcopy(base)
        forked[0]["parentSession"] = "/fixture/other.jsonl"
        cases.append(forked)
        for entries in cases:
            with self.subTest(entries=entries):
                self.assertEqual(self.evidence(entries, [task("t", "prompt")]), {})

    def test_unsupported_content_and_unknown_message_roles_fail_closed(self):
        cases = [
            user("u", None, [{"type": "image", "data": "fake", "mimeType": "image/png"}]),
            user("u", None, [{"type": "text", "text": "prompt"}, {"type": "image"}]),
            entry("u", None, "custom", "prompt"),
            entry("u", None, "future-role", "prompt"),
        ]
        for first in cases:
            with self.subTest(first=first):
                entries = [header(), first, assistant("a", "u", "answer")]
                self.assertEqual(self.evidence(entries, [task("t", "prompt")]), {})

    def test_multiple_sessions_or_symlinked_session_do_not_read_other_data(self):
        entries = [header(), user("u", None, "prompt"), assistant("a", "u", "answer")]
        self.write(entries)
        other = self.path.parent / "another.jsonl"
        other.write_text("{}\n")
        self.assertEqual(pi_evidence.extract_turns(self.home, [task("t", "prompt")]), {})
        other.unlink()
        self.path.unlink()
        external = Path(self.temp.name) / "external.jsonl"
        external.write_text("".join(json.dumps(e) + "\n" for e in entries))
        self.path.symlink_to(external)
        self.assertEqual(pi_evidence.extract_turns(self.home, [task("t", "prompt")]), {})


if __name__ == "__main__":
    unittest.main()
