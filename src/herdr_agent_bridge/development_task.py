"""Development state transitions; callers own locking, I/O and persistence."""

from __future__ import annotations

import secrets
import time
from copy import deepcopy
from dataclasses import dataclass

from herdr_agent_bridge.bridge_common import BridgeError


@dataclass(frozen=True)
class VerificationTicket:
    token: str
    round_id: str
    contract: dict[str, object]
    worktree_sha256: str
    native: dict[str, object]
    control: str
    history: list[dict[str, object]]


class DevelopmentTask:
    def __init__(self, record):
        self._record = record

    @classmethod
    def create(cls, contract):
        return cls(
            {
                "contract": contract,
                "control": "automation",
                "state": "active",
                "history": [],
                "verification": None,
            }
        )

    def to_record(self):
        return deepcopy(self._record)

    @property
    def contract(self):
        return deepcopy(self._record["contract"])

    @property
    def verifying(self):
        return (self._record.get("verification_job") or {}).get("state") == "running"

    def require_active(self) -> None:
        if self._record["state"] != "active":
            raise BridgeError("active development task required", code="development_not_active")

    def assert_input(self, task_id, operator):
        if self.verifying:
            raise BridgeError(
                "verification running; wait for its result before submitting input",
                code="verification_in_progress",
            )
        if task_id != self._record["contract"]["task_id"] or self._record["state"] != "active":
            raise BridgeError("only the original active development task may receive input")
        if (self._record["control"] == "operator") != operator:
            raise BridgeError(
                "input owner mismatch; automation is paused during takeover",
                code="input_owner_mismatch",
            )

    def invalidate(self) -> None:
        self._record["verification"] = None

    def takeover(self, actor, terminal_turn):
        self.require_active()
        if not isinstance(actor, str) or not actor.strip():
            raise BridgeError("active development task and named operator required")
        self._record["control"] = "operator"
        self.invalidate()
        self._record["history"].append(
            {
                "action": "takeover",
                "actor": actor,
                "at": time.time(),
                "terminal_turn": terminal_turn,
            }
        )

    def resume(self, review, fingerprint, terminal_turn):
        self.require_active()
        if self._record["control"] != "operator" or not review.strip():
            raise BridgeError("operator review required before resuming")
        if self.verifying:
            raise BridgeError(
                "verification stopping; observe it before resuming", code="verification_in_progress"
            )
        self._record["history"].append(
            {
                "action": "resume",
                "at": time.time(),
                "review": review,
                "fingerprint": fingerprint,
                "terminal_turn": terminal_turn,
            }
        )
        self._record["control"] = "automation"
        self.invalidate()

    def begin_verification(
        self, task, worktree_sha256: str, native: dict[str, object]
    ) -> VerificationTicket:
        self.require_active()
        if self.verifying:
            raise BridgeError(
                "verification unresolved; reconcile the original verification",
                code="verification_unresolved",
            )
        if task["observed"] != "evidence_ready":
            raise BridgeError("completed correlated development result required")
        ticket = VerificationTicket(
            secrets.token_hex(16),
            task["id"],
            self.contract,
            worktree_sha256,
            native,
            self._record["control"],
            deepcopy(self._record["history"]),
        )
        self.invalidate()
        self._record["verification_job"] = {
            "token": ticket.token,
            "round_id": ticket.round_id,
            "state": "running",
            "started_at": time.time(),
        }
        return ticket

    def verification_cancelled(self, ticket: VerificationTicket, *, closed: bool = False) -> bool:
        job = self._record.get("verification_job") or {}
        return (
            closed
            or self._record["state"] != "active"
            or job.get("token") != ticket.token
            or job.get("state") != "running"
            or bool(job.get("cancel_requested"))
            or self._record["control"] != ticket.control
            or self._record["history"] != ticket.history
        )

    def finish_verification(
        self, ticket, evidence, worktree_sha256, native, round_id, *, closed=False
    ):
        valid = (
            not self.verification_cancelled(ticket, closed=closed)
            and round_id == ticket.round_id
            and worktree_sha256 == ticket.worktree_sha256 == evidence["worktree_sha256"]
            and native == ticket.native
        )
        evidence = {**evidence, "round_id": ticket.round_id, "native": native}
        if not valid:
            evidence.update(passed=False, reason=evidence.get("reason", "verification_superseded"))
        job = self._record.get("verification_job") or {}
        if job.get("token") == ticket.token:
            job.update(
                state="cancelled"
                if evidence.get("reason") == "verification_cancelled"
                else "finished",
                finished_at=time.time(),
                reason=evidence.get("reason"),
            )
            self._record["verification"] = evidence if valid else None
        return evidence

    def abort_verification(self, token, reason="verification_check_failed"):
        job = self._record.get("verification_job") or {}
        if job.get("token") == token:
            job.update(state="failed", reason=reason, finished_at=time.time())
            self.invalidate()

    def request_cancellation(self):
        if self.verifying:
            self._record["verification_job"]["cancel_requested"] = True
            self.invalidate()

    def interrupt_verification(self):
        job = self._record.get("verification_job") or {}
        self.abort_verification(job.get("token"), "verification_interrupted")

    def decide(self, decision, actor, note, *, worktree_sha256=None, native=None):
        if (
            self._record["state"] != "active"
            or decision not in ("accept", "reject", "terminate")
            or not actor.strip()
            or not note.strip()
        ):
            raise BridgeError("explicit operator decision required")
        if decision == "accept":
            evidence = self._record.get("verification") or {}
            if (
                self.verifying
                or not evidence.get("passed")
                or worktree_sha256 != evidence.get("worktree_sha256")
                or evidence.get("native") != native
            ):
                raise BridgeError("final code/session lacks current passing verification")
            self._record["state"] = "accepted"
        elif decision == "terminate":
            self._record["state"] = "terminated"
            self.invalidate()
        else:
            self.invalidate()
        self._record["history"].append(
            {"action": decision, "actor": actor, "note": note, "at": time.time()}
        )

    def summary(self, round_ids):
        data = self._record
        return deepcopy(
            {
                "task_id": data["contract"]["task_id"],
                "goal": data["contract"]["goal"],
                "control": data["control"],
                "state": data["state"],
                "round_ids": round_ids,
                "verification": data.get("verification"),
                "verification_job": data.get("verification_job"),
                "history": data["history"],
            }
        )
