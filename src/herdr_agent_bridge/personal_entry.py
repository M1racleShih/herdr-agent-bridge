"""Trusted local binding for a Personal Agent operating across authorized projects."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from herdr_agent_bridge.bridge_common import BridgeError, run_hc
from herdr_agent_bridge.bridge_protocol import AGENTS
from herdr_agent_bridge.grok_notifications import _no_duplicate_keys, validate_owner
from herdr_agent_bridge.project_bridge import (
    NATIVE_COMMAND_REFUSALS,
    START_REFUSALS,
    SUBMIT_REFUSALS,
    Project,
    add_native_command_arguments,
    identifier,
    private_json,
    reject_constant,
)
from herdr_agent_bridge.project_registry import ProjectRegistry
from herdr_agent_bridge.readonly_contracts import PROMPTS

TASK_EFFECTS = frozenset({"start", "submit", "native-command", "verify", "close"})
MAX_BODY = 65_536


def _absolute_path(value):
    if not isinstance(value, str) or not value.isprintable():
        raise BridgeError("personal binding requires absolute paths")
    path = Path(value)
    if path.anchor != "/" or ".." in path.parts or str(path) != value:
        raise BridgeError("personal binding requires absolute paths")
    return path


def _load_binding(path):
    data = private_json(path)
    if (
        not isinstance(data, dict)
        or data.keys() != {"format", "registry", "allowed_roots", "owner", "projects"}
        or type(data["format"]) is not int
        or data["format"] != 1
        or not isinstance(data["allowed_roots"], list)
        or not isinstance(data["projects"], dict)
    ):
        raise BridgeError("invalid personal binding")
    registry = ProjectRegistry(
        _absolute_path(data["registry"]),
        allowed_roots=[_absolute_path(path) for path in data["allowed_roots"]],
    )
    owner = validate_owner(data["owner"])
    projects = {
        identifier(project_id): _absolute_path(path)
        for project_id, path in data["projects"].items()
    }
    return registry, owner, projects


def register_confirmed(binding, directory, *, name=None):
    """For local operators or trusted handlers of an authenticated user confirmation.

    Do not expose this function as an agent-controlled tool. Input is the exact
    directory the operator/user confirmed, not a natural-language assertion.
    """
    registry, _, _ = _load_binding(binding)
    return registry.register_confirmed(directory, name=name)


def _read_body(args, stream):
    if args.command not in ("start", "submit") or args.command == "start" and not args.development:
        return None
    raw = (sys.stdin.buffer if stream is None else stream).read(MAX_BODY + 1)
    try:
        if len(raw) > MAX_BODY:
            raise ValueError()
        text = raw.decode("utf-8")
        if not text.strip() or "\0" in text:
            raise ValueError()
        if args.command == "submit":
            return text
        contract = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_constant=reject_constant
        )
        if not isinstance(contract, dict):
            raise ValueError()
        return contract
    except (ValueError, RecursionError):
        raise BridgeError(
            "expected bounded UTF-8 task input on stdin", code="invalid_task_input"
        ) from None


def _task_call(args, entry, body):
    request_id = args.request_id
    if args.command == "start":
        result = entry.start(
            request_id,
            body,
            agent=args.agent,
            model=args.model,
            thinking=args.thinking,
            require_shared=True,
        )
    elif args.command == "submit":
        result = entry.submit(
            args.round_id, body, parent=args.parent_round_id, request_id=request_id
        )
    elif args.command == "status":
        result = entry.status(
            request_id, observe=args.observe, wait=10 if args.observe else 0, task_id=args.round_id
        )
    elif args.command == "result":
        result = entry.result(request_id, args.round_id)
    elif args.command == "verify":
        result = entry.verify(request_id, task_id=args.round_id, contract=args.contract)
    elif args.command == "native-command":
        result = entry.native_command(
            args.operation_id, args.native_command, args.arguments, request_id=request_id
        )
    else:  # close
        result = entry.close(request_id)
    task = result.get("task") or {}
    if task.get("task_id") is not None:
        result["round_id"] = task["task_id"]
    return entry.public({**result, "project_id": args.project_id})


def main(argv=None, *, bound_binding=None, runner=run_hc, input_stream=None):
    parser = argparse.ArgumentParser(prog="herdr-bridge personal", allow_abbrev=False)
    if bound_binding is None:
        parser.add_argument("--binding", type=Path, required=True, help="private local binding")
    else:
        parser.set_defaults(binding=Path(bound_binding))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("selector")
    if bound_binding is None:
        register = commands.add_parser("register", help="operator: register a confirmed directory")
        register.add_argument("directory", type=Path)
        register.add_argument("--name")
    for name in ("start", "submit", "native-command", "status", "result", "verify", "close"):
        task = commands.add_parser(name)
        task.add_argument("--project-id", required=True)
        task.add_argument("--request-id", required=True)
        if name in ("submit", "status", "result", "verify"):
            task.add_argument("--round-id", required=name == "submit")
        if name == "start":
            task.add_argument(
                "--development",
                action="store_true",
                help="read development contract JSON from stdin",
            )
            task.add_argument("--agent", choices=AGENTS)
            task.add_argument("--model")
            task.add_argument("--thinking", "--reasoning")
        elif name == "submit":
            task.description = (
                "Submit a UTF-8 prompt on stdin; pass the previous round when continuing."
            )
            task.add_argument("--parent-round-id")
        elif name == "status":
            task.add_argument(
                "--observe", action="store_true", help="bounded observation of the original run"
            )
        elif name == "verify":
            task.add_argument("--contract", choices=tuple(PROMPTS))
        elif name == "native-command":
            add_native_command_arguments(task)
    args = parser.parse_args(argv)
    attempted, entry = False, None
    try:
        # Read the bounded payload before opening the binding; a caller waiting
        # on stdin must not carry an old authorization into a later operation.
        body = _read_body(args, input_stream)
        registry, owner, projects = _load_binding(args.binding)
        if args.command == "register":
            attempted = True
            result = registry.register_confirmed(args.directory, name=args.name)
        elif args.command == "discover":
            result = registry.discover()
        elif args.command == "resolve":
            result = registry.resolve(args.selector)
        else:
            identifier(args.request_id)
            project = registry.require_project(args.project_id)
            config = projects.get(args.project_id)
            if config is None:
                raise BridgeError(
                    "project has no authorized execution binding", code="project_not_bound"
                )
            entry = Project(
                config,
                runner=runner,
                expected_binding=(project["root"], owner),
                expected_project_id=args.project_id,
            )
            attempted = args.command in TASK_EFFECTS or args.command == "status" and args.observe
            result = _task_call(args, entry, body)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("passed") is False else 0
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.TimeoutExpired,
        KeyboardInterrupt,
    ) as exc:
        code = exc.code if isinstance(exc, BridgeError) else "personal_operation_failed"
        uncertain = (
            attempted
            and code
            not in START_REFUSALS
            | NATIVE_COMMAND_REFUSALS
            | SUBMIT_REFUSALS
            | {
                "project_binding_mismatch",
                "verification_round_not_current",
            }
        )
        failure = {
            "code": code,
            "operation": args.command,
            "delivery": "unknown" if uncertain else "not_attempted",
        }
        if uncertain:
            failure["action"] = "inspect the original operation before retrying"
        if entry is not None:
            try:
                failure = entry.public(failure)
            except BridgeError:
                failure = {}
        print(json.dumps(failure), file=sys.stderr)
        return 1


def run_bound(binding, argv=None, *, runner=run_hc, input_stream=None):
    """Agent surface: no binding override and no permission to register a project."""
    return main(argv, bound_binding=binding, runner=runner, input_stream=input_stream)
