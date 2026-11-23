"""Preset read-only tasks and independent source/evidence checks.

These checks inspect repository source and returned task evidence; they never submit,
observe, or verify a Bridge task themselves.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .readiness import _regular_file

PROJECT = Path.cwd()
SOURCE_PATHS = ("src/herdr_agent_bridge/tools/smoke_hc.py", "tests/test_smoke_hc.py")
LEGACY_SOURCE_PATHS = ("scripts/smoke_hc.py", "tests/test_smoke_hc.py")
REQUIRED_FILES = (
    "README.md",
    "AGENTS.md",
    "docs/PROJECT.md",
    "src/herdr_agent_bridge/resources/bridge_protocol.json",
    "src/herdr_agent_bridge/agent_bridge.py",
    "src/herdr_agent_bridge/tools/accept_codex.py",
    *SOURCE_PATHS,
)
_SOURCE_LIMIT = 1_000_000

PROMPTS = {
    "analysis": (
        "Read src/herdr_agent_bridge/tools/smoke_hc.py and tests/test_smoke_hc.py. In answer return "
        "initial_task_ids (the two task identifiers in the smoke loop, in source order), "
        "blocked_error_kind (the exact error kind expected when blocked), "
        "regression_test_names (all unittest test method names, sorted), "
        "success_requires_cleanup (boolean), limitation (one sentence explaining why "
        "the local fixture does not prove real-agent or Grok integration), "
        "fixture_proves_real_agent and fixture_proves_grok (booleans), and sources "
        "(at least two objects with path, line as a 1-based integer, and quote as "
        "exact source text, on two different lines). Use only the two named files "
        "for sources. Read the files; do not run the smoke tests."
    ),
    "followup": (
        "Follow up on your previous answer. For the regression test that rejects "
        "evidence from the wrong task, return method_name, asserted_message (the "
        "exact message fragment checked), and sources (at least two exact source "
        "quotations with path, line, quote, on two different lines from "
        "src/herdr_agent_bridge/tools/smoke_hc.py or tests/test_smoke_hc.py). Also return "
        "previous_task_id, using the task identifier of our previous request from memory."
    ),
    "question": (
        "Before doing further work, ask me which language to use for the final "
        "explanation. Do not choose a language yourself. Return kind=question and "
        "answer.question."
    ),
    "clarification": (
        'Use English. Return kind=result and answer.language="English", '
        "answer.previous_question_task_id identifying your preceding question "
        "request from memory, answer.done_verifies_success (boolean), and "
        "answer.explanation: one sentence about why a done terminal state does "
        "not verify a task result."
    ),
}


def _read_source(project, relative, source_paths=SOURCE_PATHS):
    if relative not in source_paths:
        return None
    path = _regular_file(project, relative)
    if path is None:
        return None
    try:
        if path.stat().st_size > _SOURCE_LIMIT:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return None


def _record(name, passed):
    return {"check": name, "passed": bool(passed)}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _asks_for_language(value):
    if not _text(value):
        return False
    question = value.casefold()
    if "language" in question or "语言" in question or "語言" in question:
        return True
    # An explicit choice such as "English or Chinese?" also asks for a
    # language even when it omits that word.
    options = re.findall(
        r"\b(?:english|chinese|spanish|french|german|japanese|"
        r"korean|portuguese|italian)\b",
        question,
    )
    return len(set(options)) >= 2


def _parts(task):
    task = task if isinstance(task, dict) else {}
    evidence = task.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    result = evidence.get("result")
    result = result if isinstance(result, dict) else {}
    answer = result.get("answer")
    answer = answer if isinstance(answer, dict) else {}
    return task, evidence, result, answer


def _common_checks(task, kind):
    task, evidence, result, answer = _parts(task)
    task_id = task.get("id")
    return [
        _record("task and result IDs match", _text(task_id) and result.get("task_id") == task_id),
        _record("delivery received", task.get("delivery") == "received"),
        _record(
            "native thread and turn present",
            _text(evidence.get("thread_id")) and _text(evidence.get("agent_turn_id")),
        ),
        _record("agent completed", evidence.get("agent_completed") is True),
        _record(
            "expected observed state",
            task.get("observed") == ("needs_human" if kind == "question" else "evidence_ready"),
        ),
        _record(
            "result shape and kind",
            result.get("kind") == kind and isinstance(result.get("answer"), dict),
        ),
    ]


def _previous_checks(task, previous, expected_kind):
    current, current_evidence, _, _ = _parts(task)
    prev, prev_evidence, prev_result, _ = _parts(previous)
    previous_id = prev.get("id")
    previous_state = "needs_human" if expected_kind == "question" else "evidence_ready"
    previous_complete = (
        _text(previous_id)
        and prev.get("delivery") == "received"
        and prev.get("observed") == previous_state
        and prev_evidence.get("agent_completed") is True
        and prev_result.get("task_id") == previous_id
        and prev_result.get("kind") == expected_kind
        and isinstance(prev_result.get("answer"), dict)
        and (expected_kind != "question" or prev.get("verified_success") is False)
    )
    same_thread = (
        _text(current_evidence.get("thread_id"))
        and _text(prev_evidence.get("thread_id"))
        and current_evidence["thread_id"] == prev_evidence["thread_id"]
    )
    distinct_turn = (
        _text(current_evidence.get("agent_turn_id"))
        and _text(prev_evidence.get("agent_turn_id"))
        and current_evidence["agent_turn_id"] != prev_evidence["agent_turn_id"]
    )
    return [
        _record("previous task has correlated completed evidence", previous_complete),
        _record(
            "parent matches previous task",
            _text(previous_id)
            and current.get("parent") == previous_id
            and current.get("id") != previous_id,
        ),
        _record("same native thread and distinct turn", same_thread and distinct_turn),
    ]


def _quote_checks(answer, sources, source_paths=SOURCE_PATHS):
    quotations = answer.get("sources")
    if not isinstance(quotations, list) or not 2 <= len(quotations) <= 100:
        return [
            _record("source quotations have valid shape", False),
            _record("at least two distinct valid source lines", False),
        ]
    checks, valid_lines = [], set()
    for index, quotation in enumerate(quotations, 1):
        if not isinstance(quotation, dict):
            checks.append(_record("source quotation " + str(index), False))
            continue
        path, line, quote = quotation.get("path"), quotation.get("line"), quotation.get("quote")
        content = sources.get(path) if isinstance(path, str) and path in source_paths else None
        valid = False
        if content is not None and type(line) is int and isinstance(quote, str):
            lines = content.splitlines()
            valid = (
                1 <= line <= len(lines)
                and len(quote.strip()) >= 8
                and len(quote) <= 1000
                and quote in lines[line - 1]
            )
            if valid:
                valid_lines.add((path, line))
        checks.append(_record("source quotation " + str(index), valid))
    checks.append(_record("at least two distinct valid source lines", len(valid_lines) >= 2))
    return checks


def _ast_equal(node, expression):
    return ast.dump(node, include_attributes=False) == ast.dump(
        ast.parse(expression, mode="eval").body, include_attributes=False
    )


def _source_facts(sources, source_paths=SOURCE_PATHS):
    """Extract the smoke facts and the named wrong-task assertion from ASTs."""
    try:
        smoke = ast.parse(sources[source_paths[0]])
        tests = ast.parse(sources[source_paths[1]])
    except (KeyError, SyntaxError, TypeError, ValueError, RecursionError):
        return {}
    ids, blocked, passed_exprs, clean_exprs = [], [], [], []
    for node in ast.walk(smoke):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "task"
        ):
            try:
                value = ast.literal_eval(node.iter)
            except (ValueError, TypeError, SyntaxError):
                continue
            if isinstance(value, list) and len(value) == 2 and all(_text(item) for item in value):
                ids.append(value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "require"
            and node.args
        ):
            comparison = node.args[0]
            if (
                isinstance(comparison, ast.Compare)
                and len(comparison.ops) == 1
                and isinstance(comparison.ops[0], ast.Eq)
                and len(comparison.comparators) == 1
                and _ast_equal(
                    comparison.left, "json.loads(refused.stderr).get('error', {}).get('kind')"
                )
                and isinstance(comparison.comparators[0], ast.Constant)
                and _text(comparison.comparators[0].value)
            ):
                blocked.append(comparison.comparators[0].value)
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "result"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "passed"
            ):
                passed_exprs.append(
                    _ast_equal(node.value, "completed and clean and failure is None")
                )
            if isinstance(target, ast.Name) and target.id == "clean":
                clean_exprs.append(node.value)
    names = sorted(
        node.name
        for node in ast.walk(tests)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )
    method = "test_optimized_run_rejects_wrong_task_evidence"
    methods = [
        node
        for node in ast.walk(tests)
        if isinstance(node, ast.FunctionDef) and node.name == method
    ]
    messages = []
    mismatch_scenario = False
    if len(methods) == 1:
        for node in ast.walk(methods[0]):
            if (
                isinstance(node, ast.Call)
                and _ast_equal(node.func, "self.run_scenario")
                and len(node.args) == 1
                and _ast_equal(node.args[0], "'mismatch'")
            ):
                mismatch_scenario = True
            if (
                isinstance(node, ast.Call)
                and _ast_equal(node.func, "self.assertIn")
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and _text(node.args[0].value)
                and _ast_equal(node.args[1], "result['error']")
            ):
                messages.append(node.args[0].value)
    return {
        "initial_task_ids": ids[0] if len(ids) == 1 else None,
        "blocked_error_kind": blocked[0] if len(blocked) == 1 else None,
        # Fail closed if another assignment could promote a failed cleanup.
        "success_requires_cleanup": passed_exprs == [True]
        and sum(
            _ast_equal(expr, "all(c.get('exit_code') == 0 for c in cleanup)")
            for expr in clean_exprs
        )
        == 1
        and all(
            _ast_equal(expr, "all(c.get('exit_code') == 0 for c in cleanup)")
            or isinstance(expr, ast.Constant)
            and expr.value is False
            for expr in clean_exprs
        ),
        "regression_test_names": names if names else None,
        "method_name": method if mismatch_scenario and len(messages) == 1 else None,
        "asserted_message": messages[0] if mismatch_scenario and len(messages) == 1 else None,
    }


def check_task(contract, task, previous=None, project=PROJECT):
    """Return independent check records without modifying task or Bridge state."""
    if not isinstance(contract, str) or contract not in PROMPTS:
        raise ValueError("unknown read-only contract")
    kind = "question" if contract == "question" else "result"
    current, evidence, result, answer = _parts(task)
    checks = _common_checks(task, kind)
    if contract in ("analysis", "followup"):
        # Historic rounds retain their submitted source contract; never let an
        # answer choose the inputs against which it will be verified. Never rewrite a citation
        # or substitute the installed implementation for the original project.
        submitted = current.get("submitted_text", "")
        legacy = (
            isinstance(submitted, str)
            and LEGACY_SOURCE_PATHS[0] in submitted
            and SOURCE_PATHS[0] not in submitted
        )
        source_paths = LEGACY_SOURCE_PATHS if legacy else SOURCE_PATHS
        sources = {path: _read_source(project, path, source_paths) for path in source_paths}
        facts = _source_facts(sources, source_paths)
        if contract == "analysis":
            limitation = answer.get("limitation")
            words = limitation.lower() if isinstance(limitation, str) else ""
            checks.extend(
                [
                    _record(
                        "initial task IDs match smoke source",
                        facts.get("initial_task_ids") is not None
                        and answer.get("initial_task_ids") == facts["initial_task_ids"],
                    ),
                    _record(
                        "blocked error kind matches source",
                        facts.get("blocked_error_kind") is not None
                        and answer.get("blocked_error_kind") == facts["blocked_error_kind"],
                    ),
                    _record(
                        "regression methods match Python AST",
                        facts.get("regression_test_names") is not None
                        and answer.get("regression_test_names") == facts["regression_test_names"],
                    ),
                    _record(
                        "cleanup is part of success",
                        facts.get("success_requires_cleanup") is True
                        and answer.get("success_requires_cleanup") is True,
                    ),
                    _record(
                        "explains fixture evidence limitation",
                        len(words.strip()) >= 20
                        and ("fixture" in words or "inert" in words)
                        and "agent" in words
                        and "grok" in words
                        and any(
                            negation in words
                            for negation in (
                                " not ",
                                "cannot",
                                "can't",
                                "neither",
                                "without",
                                " no ",
                                "never",
                            )
                        )
                        and answer.get("fixture_proves_real_agent") is False
                        and answer.get("fixture_proves_grok") is False,
                    ),
                ]
            )
        else:
            prev, _, _, _ = _parts(previous)
            checks.extend(_previous_checks(task, previous, "result"))
            checks.extend(
                [
                    _record(
                        "wrong-task test and assertion match source",
                        facts.get("method_name") is not None
                        and facts.get("asserted_message") is not None
                        and answer.get("method_name") == facts["method_name"]
                        and answer.get("asserted_message") == facts["asserted_message"],
                    ),
                    _record(
                        "previous request remembered",
                        _text(prev.get("id")) and answer.get("previous_task_id") == prev.get("id"),
                    ),
                ]
            )
        checks.extend(_quote_checks(answer, sources, source_paths))
    elif contract == "question":
        checks.extend(
            [
                _record(
                    "question asks for language selection",
                    _asks_for_language(answer.get("question")),
                ),
                _record(
                    "question not promoted to verified success",
                    current.get("verified_success") is False,
                ),
            ]
        )
        if previous is not None:
            checks.extend(_previous_checks(task, previous, "result"))
    else:
        prev, _, _, _ = _parts(previous)
        checks.extend(_previous_checks(task, previous, "question"))
        checks.extend(
            [
                _record("clarification uses English", answer.get("language") == "English"),
                _record(
                    "previous question remembered",
                    _text(prev.get("id"))
                    and answer.get("previous_question_task_id") == prev.get("id"),
                ),
                _record(
                    "done does not verify success", answer.get("done_verifies_success") is False
                ),
                _record("explanation present", _text(answer.get("explanation"))),
            ]
        )
    return checks
