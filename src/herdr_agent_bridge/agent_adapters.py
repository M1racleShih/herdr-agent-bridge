"""Agent configuration, launch argv and native evidence without execution state."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from pathlib import Path

from herdr_agent_bridge import codex_native
from herdr_agent_bridge.bridge_common import BridgeError, save
from herdr_agent_bridge.bridge_protocol import (
    AGENTS,
    CODEX_THINKING_LEVELS,
    PI_THINKING_LEVELS,
    valid_agent_model,
)
from herdr_agent_bridge.resource_paths import resource_path

NATIVE_ENV_KEYS = (
    "HOME",
    "PATH",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_CACHE_HOME",
    "HC_SESSION",
    "HERDR_APP_HOME",
    "PI_CODING_AGENT_DIR",
    "CODEX_HOME",
    "CODEX_SQLITE_HOME",
)

PARENT_AGENT_KEYS = (
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_TOOL_CALL_ID",
    "PI_SESSION_ID",
    "PI_SESSION_FILE",
)


def validate_agent_selection(agent, model, thinking, runtime, *, project=None):
    if agent not in AGENTS:
        raise BridgeError("unsupported coding agent")
    if model is not None and not valid_agent_model(agent, model, isolated=runtime == "isolated"):
        message = (
            "native Pi requires provider/model when overridden"
            if agent == "pi" and runtime == "native"
            else "invalid coding agent model identifier"
        )
        raise BridgeError(message, code="execution_policy_invalid")
    levels = PI_THINKING_LEVELS if agent == "pi" else CODEX_THINKING_LEVELS
    if thinking is not None and (not isinstance(thinking, str) or thinking not in levels):
        raise BridgeError(
            "unsupported thinking level or coding agent", code="execution_policy_invalid"
        )
    if project is not None and runtime == "native" and agent == "codex":
        codex_native.validate_model_effort(
            model, thinking, {"project": project, "native_env": os.environ}
        )


def build_agent_command(data, root, *, session_file=None, session_id=None, startup_token=None):
    """Build agent argv; lifecycle and delivery stay with Bridge."""
    root = Path(root)
    if data.get("runtime") == "native":
        command = ["env"]
        # hc's process environment comes from the core, not its client.
        for key in (*PARENT_AGENT_KEYS, *NATIVE_ENV_KEYS):
            if key in PARENT_AGENT_KEYS or data["native_env"].get(key) is None:
                command.extend(["-u", key])
        command.extend(
            key + "=" + value for key, value in data["native_env"].items() if value is not None
        )
        if data.get("agent") == "codex":
            command.append("codex")
            if session_id is not None:
                command.append("resume")
            command.extend(
                [
                    "--no-daemon",
                    "--no-alt-screen",
                    "--cd",
                    data["project"],
                    "-c",
                    codex_native.STATUS_LINE,
                ]
            )
            if data.get("model"):
                command.extend(["-m", data["model"]])
            if data.get("thinking"):
                command.extend(["-c", "model_reasoning_effort=" + json.dumps(data["thinking"])])
            if session_id is not None:
                command.append(session_id)
            return command
        command.extend(
            [
                "PI_PROGRAM_STATUS=1",
                "HAB_PI_SELECTION_FILE=" + str(root / "pi-selection.json"),
                "HAB_PI_RESOURCES_FILE=" + str(root / "pi-resources.json"),
                "HAB_PI_SETTLED_FILE=" + str(root / "pi-settled.json"),
                "HAB_PI_MODEL=" + (data.get("model") or ""),
                "HAB_PI_THINKING=" + (data.get("thinking") or ""),
                "pi",
                "--session-dir",
                str(Path(data["scratch"]) / "pi" / "sessions"),
                "-e",
                str(resource_path("pi_native_observer.mjs")),
            ]
        )
        if data.get("model"):
            provider, model = data["model"].split("/", 1)
            command.extend(["--provider", provider, "--model", model])
        if data.get("thinking"):
            command.extend(["--thinking", data["thinking"]])
        if session_file is not None:
            command.extend(["--session", str(session_file)])
            # This variable belongs to the env prefix, before the executable.
            command.insert(command.index("pi"), "HAB_PI_RECOVERY_TOKEN=" + startup_token)
    elif data.get("agent", "codex") == "codex":
        command = [
            "codex",
            "--no-daemon",
            "--no-alt-screen",
            "-s",
            "read-only",
            "-a",
            "on-request",
            "-m",
            data["model"],
        ]
    else:
        home = Path(data["scratch"]) / "pi"
        if (
            data.get("pi_auth_mode") == "native_shared"
            or data.get("development")
            or data.get("thinking") is not None
        ):
            command = [
                "node",
                str(resource_path("pi_oauth_session.mjs")),
                shutil.which("pi") or "pi",
                str(home),
                data["project"],
                data["model"],
            ]
            if data.get("development"):
                command.append("development")
            if data.get("thinking") is not None:
                command.extend(
                    [
                        "--thinking",
                        data["thinking"],
                        "--selection-file",
                        str(root / "pi-selection.json"),
                    ]
                )
        else:
            command = [
                "pi",
                "--tui-mode",
                "regular",
                "--no-approve",
                "--no-extensions",
                "--no-mcp",
                "--no-skills",
                "--no-prompt-templates",
                "--no-themes",
                "--tools",
                "read,grep,find,ls",
                "--session-dir",
                str(home / "sessions"),
                "--model",
                data["model"],
            ]
    return command


def toml_value(value):
    # Only used for a local subset of the user's existing provider configuration.
    if isinstance(value, dict):
        return (
            "{" + ", ".join(json.dumps(k) + " = " + toml_value(v) for k, v in value.items()) + "}"
        )
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, bool):
        return str(value).lower()
    return json.dumps(value)


def prepare_agent_home(destination, source, project, model, thinking=None):
    # Authentication is copied locally, never returned or committed. No plugins,
    # hooks, MCP servers, global instructions or unrelated project trust imported.
    try:
        import tomllib
    except ImportError as exc:
        raise BridgeError("agent bridge requires Python 3.11+ (tomllib)") from exc
    destination.mkdir(mode=0o700)
    config_path = source / "config.toml"
    config = tomllib.loads(config_path.read_text()) if config_path.is_file() else {}
    selected = {
        k: config[k]
        for k in ("model_provider", "model_providers", "service_tier", "shell_environment_policy")
        if k in config
    }
    # Reuse a pre-existing trust decision for this exact directory only.
    prior_trust = config.get("projects", {}).get(str(project))
    if prior_trust:
        selected["projects"] = {str(project): prior_trust}
    selected.update(
        model=model,
        model_reasoning_effort=thinking or "max",
        sandbox_mode="read-only",
        approval_policy="on-request",
    )
    target = destination / "config.toml"
    target.write_text("\n".join(k + " = " + toml_value(v) for k, v in selected.items()) + "\n")
    target.chmod(0o600)
    auth = source / "auth.json"
    if auth.is_file():
        shutil.copyfile(auth, destination / "auth.json")
        (destination / "auth.json").chmod(0o600)


def _pi_json(path, comments=False):
    text = path.read_text(encoding="utf-8-sig")
    if comments:
        # Pi accepts JSON comments in models.json. Keep quoted URLs and escaped
        # quotes intact, and replace comments with whitespace to avoid merging
        # adjacent tokens. Unterminated comments remain invalid JSON.
        text = re.sub(
            r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/',
            lambda match: match[0] if match[0].startswith('"') else " ",
            text,
        )
    return json.loads(text)


def prepare_pi_home(destination, source, model=None):
    """Copy only the selected provider and model into a private Pi agent dir.

    No agent-home packages, extensions, MCP, trust, instructions or previous
    sessions are imported. Project/ancestor context files still load normally.
    OAuth uses Pi's original native auth store through its public SDK, so token
    rotation stays under Pi's normal lock and survives scratch cleanup.
    Pi permits shell-derived credentials; those need separate review
    and are refused here rather than executing commands from user config.
    """
    destination.mkdir(mode=0o700)
    path = source / "settings.json"
    settings = _pi_json(path) if path.is_file() else {}
    if not isinstance(settings, dict):
        raise BridgeError("invalid pi settings")
    provider = settings.get("defaultProvider")
    selected_model = model or settings.get("defaultModel")
    if model and isinstance(selected_model, str) and "/" in selected_model:
        provider, selected_model = selected_model.split("/", 1)
    if (
        not isinstance(provider, str)
        or not provider.strip()
        or not isinstance(selected_model, str)
        or not selected_model.strip()
        or any(c.isspace() or ord(c) < 32 for c in provider + selected_model)
        or provider.startswith("-")
        or selected_model.startswith("-")
    ):
        raise BridgeError(
            "pi requires --model provider/model or saved defaultProvider/defaultModel"
        )
    selected = {
        key: settings[key]
        for key in (
            "defaultThinkingLevel",
            "lastChangelogVersion",
            "httpProxy",
            "httpIdleTimeoutMs",
            "websocketConnectTimeoutMs",
            "transport",
        )
        if key in settings
    }
    selected.update(
        defaultProvider=provider,
        defaultModel=selected_model,
        compaction={"enabled": False},
        retry={"enabled": False, "provider": {"maxRetries": 0}},
        quietStartup=True,
    )
    save(destination / "settings.json", selected)

    def reject_shell_values(value):
        if isinstance(value, str) and value.lstrip().startswith("!"):
            raise BridgeError("pi shell-derived credentials are not supported by this adapter")
        if isinstance(value, dict):
            for item in value.values():
                reject_shell_values(item)
        elif isinstance(value, list):
            for item in value:
                reject_shell_values(item)

    path = source / "models.json"
    if path.is_file():
        models = _pi_json(path, comments=True)
        if not isinstance(models, dict) or not isinstance(models.get("providers", {}), dict):
            raise BridgeError("invalid pi models configuration")
        config = models.get("providers", {}).get(provider)
        if config is not None:
            if not isinstance(config, dict):
                raise BridgeError("invalid pi provider configuration")
            # Pi also resolves per-model and modelOverrides header values.
            # Check the entire copied provider tree before any launch.
            reject_shell_values(config)
            save(destination / "models.json", {"providers": {provider: config}})
    path = source / "auth.json"
    if path.is_file():
        auth = _pi_json(path)
        if not isinstance(auth, dict):
            raise BridgeError("invalid pi authentication configuration")
        if provider in auth:
            credential = auth[provider]
            if not isinstance(credential, dict):
                raise BridgeError("invalid pi authentication credential")
            if credential.get("type") == "oauth":
                if (
                    not all(
                        isinstance(credential.get(k), str) and credential[k]
                        for k in ("access", "refresh")
                    )
                    or isinstance(credential.get("expires"), bool)
                    or not isinstance(credential.get("expires"), (int, float))
                    or not math.isfinite(credential["expires"])
                ):
                    raise BridgeError("invalid pi OAuth credential")
                # ModelRuntime checks availability across the native store.
                # Refuse configured shell commands before sharing that store.
                for stored in auth.values():
                    if isinstance(stored, dict) and stored.get("type") == "api_key":
                        reject_shell_values(stored)
                save(destination / "auth-source.json", {"path": str(path.resolve())})
            elif credential.get("type") == "api_key":
                reject_shell_values(credential)
                save(destination / "auth.json", {provider: credential})
            else:
                raise BridgeError("unsupported pi authentication credential type")
    return provider + "/" + selected_model


def prepare_isolated_agent(data, root, source_home=None):
    """Prepare a private agent home, updating Pi selection and its journal."""
    if data.get("runtime") == "native":
        return
    root = Path(root)
    scratch = Path(data["scratch"])
    project = Path(data["project"])
    model = data.get("model")
    if data.get("agent", "codex") == "codex":
        source = Path(source_home or os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        prepare_agent_home(scratch / "codex", source, project, model, data.get("thinking"))
    else:
        source = Path(
            source_home or os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent"))
        )
        data["model"] = prepare_pi_home(scratch / "pi", source, model)
        data["pi_auth_mode"] = (
            "native_shared"
            if (scratch / "pi" / "auth-source.json").is_file()
            else "isolated_api_key"
        )
        save(root / "journal.json", data)
        thinking = data.get("thinking")
        if thinking is not None:
            settings_path = scratch / "pi" / "settings.json"
            settings = json.loads(settings_path.read_text())
            settings["defaultThinkingLevel"] = thinking
            save(settings_path, settings)
        contract = (data.get("development") or {}).get("contract")
        if contract is not None:
            save(
                scratch / "pi" / "development-policy.json",
                {
                    "project": str(project),
                    "allowed_paths": contract["allowed_paths"],
                    "checks": contract["checks"],
                    "journal": str(root / "journal.json"),
                },
            )


def extract_turns(home, tasks):
    """Match exact submitted user text and final assistant answer in same native turn.

    Only the isolated home is read. Multiple threads, partial JSONL and missing
    turn metadata fail closed. Prompt echoes and commentary are not results.
    """
    paths = list((home / "sessions").rglob("*.jsonl"))
    if len(paths) != 1:
        return {}
    thread, matched, finals, completed = None, {}, {}, {}
    try:
        lines = paths[0].read_text().splitlines()
        ambiguous = set()
        for index, line in enumerate(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                if index != len(lines) - 1:
                    return {}
                continue  # A live writer can leave a partial last event.
            payload = entry.get("payload", {})
            if entry.get("type") == "session_meta":
                identity = payload.get("id")
                if thread is not None and thread != identity:
                    return {}
                thread = identity
            if entry.get("type") == "response_item":
                turn = payload.get("internal_chat_message_metadata_passthrough", {}).get("turn_id")
                if not turn:
                    continue
                content = "\n".join(c.get("text", "") for c in payload.get("content", []))
                if payload.get("role") == "user":
                    for task in tasks:
                        if content == task["submitted_text"]:
                            if task["id"] in matched:
                                ambiguous.add(task["id"])
                            matched[task["id"]] = (turn, index)
                elif payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                    finals[turn] = (content, index)
            if entry.get("type") == "event_msg" and payload.get("type") == "task_complete":
                completed[payload.get("turn_id")] = index
    except OSError:
        return {}
    result = {}
    for key, (turn, user_at) in matched.items():
        if not thread or key in ambiguous:
            continue
        answer, final_at = finals.get(turn, (None, -1))
        complete_at = completed.get(turn, -1)
        ordered = user_at < final_at < complete_at
        result[key] = {
            "thread_id": thread,
            "agent_turn_id": turn,
            "answer": answer if user_at < final_at else None,
            "agent_completed": ordered,
        }
    return result
