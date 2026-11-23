"""Read the current Codex TUI selection and locate only its bound rollout."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from herdr_agent_bridge.bridge_common import BridgeError

THREAD_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
STATUS_LINE = 'tui.status_line=["session-id","model-with-reasoning","fast-mode"]'
_FOOTER = re.compile(rf"(?P<session>{THREAD_ID}) · (?P<selection>[^·]+) · (?P<tier>[^·]+)")
_HINT = re.compile(r"(?:[←→↑↓]+ for agents(?: · )?|\? for shortcuts(?:\s|$))")


def home(data):
    environment = data["native_env"]
    value = environment.get("CODEX_HOME")
    if not value:
        value = Path(environment.get("HOME") or Path.home()) / ".codex"
    path = Path(value).expanduser()
    return (Path(data["project"]) / path).resolve()


def _slug(label):
    # Codex displays catalog names such as GPT-6.1 Terra and GPT-6.2-Sol.
    return "-".join(label.lower().split())


def _catalog(data):
    try:
        value = json.loads((home(data) / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models = value.get("models") if isinstance(value, dict) else None
    return (
        [model for model in models if isinstance(model, dict)] if isinstance(models, list) else []
    )


def validate_model_effort(model, thinking, data):
    if model is None or thinking is None:
        return
    project = Path(data["project"]).resolve()
    configs = [home(data) / "config.toml"] + [
        directory / ".codex" / "config.toml" for directory in (project, *project.parents)
    ]
    # Custom providers and profiles can advertise a different effort catalog.
    for path in configs:
        if not path.is_file():
            continue
        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if config.get("model_provider", "openai") != "openai" or config.get("profile"):
            return
    for item in _catalog(data):
        if item.get("slug") == model and isinstance(item.get("supported_reasoning_levels"), list):
            supported = {
                level.get("effort")
                for level in item["supported_reasoning_levels"]
                if isinstance(level, dict)
            }
            if supported and thinking not in supported:
                raise BridgeError(
                    f"Codex model {model} does not support reasoning level {thinking}",
                    code="execution_policy_invalid",
                )
            return


def _viewport(screen):
    if (
        not isinstance(screen, dict)
        or screen.get("complete") is not True
        or screen.get("how") != "screen"
        or not isinstance(screen.get("text"), str)
    ):
        return []
    return [line.strip() for line in screen["text"].splitlines() if line.strip()]


def trust_required(selection):
    return selection is not None and selection.get("reason") in {
        "codex_folder_trust_required",
        "codex_hook_trust_required",
    }


def _selection(lines, data):
    # Only accept the active viewport's bottom status line. Historical mentions of
    # a thread, including assistant output, do not bind this terminal.
    if lines and _HINT.match(lines[-1]):
        lines.pop()
    if not lines:
        return None
    match = _FOOTER.fullmatch(lines[-1])
    if match is None:
        return None
    tier = match["tier"].strip().lower()
    selection = match["selection"].strip()
    if selection.endswith(" " + tier):
        selection = selection[: -len(tier) - 1]
    fields = selection.rsplit(" ", 1)
    if len(fields) != 2 or not re.fullmatch(r"[a-z][a-z0-9_-]*", fields[1]):
        return None
    display_name, thinking = fields
    models = {
        model["slug"]
        for model in _catalog(data)
        if model.get("display_name") == display_name and isinstance(model.get("slug"), str)
    }
    if len(models) > 1:
        return None
    model = next(iter(models), _slug(display_name))
    return {
        "state": "ready",
        "session_id": match["session"],
        "model": model,
        "thinking": thinking,
        "service_tier": tier,
        "native_evidence": "pending",
    }


def inspect(bridge, terminal, screen=None):
    data = bridge.data
    if terminal.get("state") != "live" or terminal.get("cwd") != data["project"]:
        return {"state": "pending", "reason": "codex_terminal_pending"}
    status = terminal.get("status") or {}
    foreground = terminal.get("foreground") or {}
    if (
        foreground.get("name") not in (None, "codex")
        or foreground.get("cwd") not in (None, data["project"])
        or STATUS_LINE not in (foreground.get("argv") or terminal.get("argv") or [])
    ):
        return (
            {"state": "failed", "reason": "codex_process_mismatch"}
            if status.get("app") == "codex"
            else {"state": "pending", "reason": "codex_terminal_pending"}
        )
    if screen is None:
        screen = bridge.hc("read", "--json", "--lines", "0", terminal["address"])
    lines = _viewport(screen)
    if (
        "Folder access" in lines
        and data["project"] in lines
        and any(line.startswith("Trust this folder?") for line in lines)
        and any(line.endswith("1. Trust and continue") for line in lines)
        and any(line.endswith("2. Back to Agent Command Center") for line in lines)
    ):
        return {"state": "pending", "reason": "codex_folder_trust_required"}
    if "Hooks need review" in lines and [line.removeprefix("› ") for line in lines[-4:]] == [
        "1. Review hooks",
        "2. Trust all and continue",
        "3. Continue without trusting (hooks won't run)",
        "enter confirm · esc skip",
    ]:
        return {"state": "pending", "reason": "codex_hook_trust_required"}
    if status.get("app") != "codex":
        return {"state": "pending", "reason": "codex_terminal_pending"}
    selection = _selection(lines, data)
    if selection is None:
        return {"state": "pending", "reason": "codex_status_line_unavailable"}
    binding = data.get("codex_binding")
    if binding is not None and (
        binding["session_id"] != selection["session_id"]
        or binding["address"] != terminal["address"]
    ):
        return {"state": "failed", "reason": "codex_thread_changed"}
    reasoning = "default" if data.get("thinking") == "none" else data.get("thinking")
    if (
        data.get("model") is not None
        and data["model"] != selection["model"]
        or reasoning is not None
        and reasoning != selection["thinking"]
    ):
        return {"state": "failed", "reason": "codex_selection_mismatch"}
    if binding is None:
        data["codex_binding"] = {
            "session_id": selection["session_id"],
            "address": terminal["address"],
        }
    return selection


def history(data):
    """Identify an exact rollout filename; never read other thread contents."""
    binding = data.get("codex_binding")
    if not binding:
        return None
    thread = binding.get("session_id")
    if not isinstance(thread, str) or not re.fullmatch(THREAD_ID, thread):
        raise BridgeError("invalid Codex thread binding", code="codex_binding_invalid")
    directory = (home(data) / "sessions").resolve()
    filename = re.compile(
        rf"rollout-\d{{4}}-\d\d-\d\dT\d\d-\d\d-\d\d-{thread}(?:_{THREAD_ID})?\.jsonl"
    )
    paths = [
        path
        for path in directory.glob(f"*/*/*/rollout-*-{thread}*.jsonl")
        if filename.fullmatch(path.name)
    ]
    if len(paths) > 1:
        raise BridgeError(
            "multiple rollouts for bound Codex thread", code="codex_rollout_ambiguous"
        )
    if not paths:
        return None
    path = paths[0]
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory):
        raise BridgeError("invalid bound Codex rollout path", code="codex_rollout_invalid")
    return path
