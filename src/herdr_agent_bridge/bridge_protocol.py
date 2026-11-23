"""Shared execution policy and public protocol, independent of runtime state."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Literal, TypedDict, cast

from herdr_agent_bridge.agent_capabilities import AGENT_CAPABILITIES
from herdr_agent_bridge.bridge_common import BridgeError, digest
from herdr_agent_bridge.resource_paths import resource_path

_PROTOCOL = json.loads(resource_path("bridge_protocol.json").read_text())
AGENTS = tuple(AGENT_CAPABILITIES)
PI_THINKING_LEVELS = tuple(_PROTOCOL["thinking_levels"])
CODEX_THINKING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
DEVELOPMENT_MODEL = _PROTOCOL["development_model"]


class ModelSelection(TypedDict):
    model: str
    thinking: str


class ModelOverrides(TypedDict):
    model: str | None
    thinking: str | None


class RoundResult(TypedDict):
    task_id: str
    kind: Literal["result", "question"]
    answer: dict[str, object]


def decode_result(text: str, task_id: str) -> RoundResult:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    result = json.loads(text)
    if (
        not isinstance(result, dict)
        or result.get("task_id") != task_id
        or result.get("kind") not in ("result", "question")
        or not isinstance(result.get("answer"), dict)
    ):
        raise ValueError("answer contract mismatch")
    return cast(RoundResult, result)


def verification_basis(evidence: object) -> str | None:
    """Identify the native turn and exact answer verified by read-only checks."""
    if (
        not isinstance(evidence, dict)
        or evidence.get("agent_completed") is not True
        or not isinstance(evidence.get("thread_id"), str)
        or not isinstance(evidence.get("agent_turn_id"), str)
        or not isinstance(evidence.get("answer"), str)
    ):
        return None
    return digest(
        json.dumps(
            [
                evidence["thread_id"],
                evidence["agent_turn_id"],
                evidence.get("final_message_id"),
                evidence["answer"],
            ]
        )
    )


def selection_agent(agent: object) -> str:
    if not isinstance(agent, str) or agent not in AGENTS:
        raise BridgeError("unsupported coding agent", code="execution_policy_invalid")
    return agent


def valid_agent_model(agent: str, model: object, *, isolated: bool = False) -> bool:
    if not isinstance(model, str):
        return False
    if agent == "codex":
        return re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", model) is not None
    if agent != "pi":
        return False
    if isolated:
        return not model.startswith("-") and all(c.isprintable() and not c.isspace() for c in model)
    return (
        re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.:/-]*", model)
        is not None
    )


def policy(value: object, *, agent: str = "pi") -> ModelSelection:
    selection_agent(agent)
    if not isinstance(value, dict) or set(value) != {"model", "thinking"}:
        raise BridgeError("invalid execution policy fields", code="execution_policy_invalid")
    model, thinking = value["model"], value["thinking"]
    if agent == "pi":
        valid_model = (
            isinstance(model, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:/-]+", model)
            and not any(part.startswith("-") for part in model.split("/", 1))
        )
        levels = PI_THINKING_LEVELS
        requirement = "explicit provider/model and supported Pi thinking level required"
    else:
        valid_model = valid_agent_model(agent, model)
        levels = CODEX_THINKING_LEVELS
        requirement = "explicit model ID and supported Codex reasoning effort required"
    if not valid_model or thinking not in levels:
        raise BridgeError(
            requirement,
            code="execution_policy_invalid",
        )
    return {"model": model, "thinking": thinking}


@dataclass(frozen=True)
class ExecutionPolicy:
    requested: ModelOverrides
    resolved: ModelSelection
    sources: dict[str, str]

    @classmethod
    def resolve(
        cls,
        defaults: ModelSelection,
        *,
        agent: str = "pi",
        model: str | None = None,
        thinking: str | None = None,
        default_source: str = "project_default",
    ) -> ExecutionPolicy:
        overrides: ModelOverrides = {"model": model, "thinking": thinking}
        return cls(
            overrides,
            policy(
                {
                    key: value if value is not None else dict(defaults)[key]
                    for key, value in overrides.items()
                },
                agent=agent,
            ),
            {
                key: "task_override" if value is not None else default_source
                for key, value in overrides.items()
            },
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


PUBLIC_FIELDS = frozenset(
    """project_id defaults checks notification_configured active_request
    model thinking applies_to running_session_changed request_id execution_policy requested
    resolved sources format run_id run agent closed launch execution session address state
    model_selection actual session_id native_evidence service_tier
    status task task_id parent delivery observed verified_success evidence thread_id agent_turn_id
    agent_completed result input_ready development goal control round_ids verification history
    event_ids event_id monitor token pid seconds started_at finished_at reason passed check
    notification acknowledged receipt relay delivery_id monitor_error cleanup contract acceptance allowed_paths
    baseline changed outside stable sha256 mode link argv timeout returncode stdout stderr
    decision actor note fingerprint review error detail action kind answer references left_descendants
    created_at checks_passed diff files scope violations elapsed timed_out changed_paths
    outside_scope stable_during_checks worktree_sha256 collected_at round_id native args
    reconciled exit_code output output_truncated control_result terminal terminal_turn at
    sessions operator_text configured watch_status operation runtime backend name next_action monitor_cleanup
    app app_space space binding pending visible tab pane verification_job diagnostic cancel_requested cancelled code
    interrupt requested_at before_turn agent_aborted agent_lost recovery observation subscription
    native_operation operation_id command arguments
    connections reconnects snapshots events duplicates last_seq""".split()
)


def public_task(task):
    if task is None:
        return None
    evidence = task.get("evidence", {})
    basis = verification_basis(evidence)
    return {
        "task_id": task["id"],
        "parent": task.get("parent"),
        "delivery": task["delivery"],
        "observed": task["observed"],
        "verified_success": (
            task.get("verified_success") is True
            and basis is not None
            and task.get("verification_basis") == basis
        ),
        "evidence": {
            **{
                key: evidence.get(key)
                for key in ("thread_id", "agent_turn_id", "agent_completed", "result")
            },
            **{
                key: evidence[key]
                for key in ("agent_aborted", "agent_lost", "service_tier")
                if key in evidence
            },
        },
        **(
            {
                "interrupt": {
                    key: value for key, value in task["interrupt"].items() if key != "receipt"
                }
            }
            if task.get("interrupt")
            else {}
        ),
        **({"diagnostic": task["diagnostic"]} if task.get("diagnostic") else {}),
    }
