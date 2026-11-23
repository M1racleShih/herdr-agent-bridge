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
