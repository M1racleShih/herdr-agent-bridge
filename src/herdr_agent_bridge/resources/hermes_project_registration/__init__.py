"""Hermes plugin: register an exact directory only from the viewer confirmation card."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

SETTINGS = Path(__file__).resolve().with_name("settings.json")
CONFIRM_SECONDS = 120
_SETTINGS_FIELDS = {
    "format",
    "profile",
    "session_id",
    "chat_id",
    "user_id",
    "bridge_python",
    "binding",
}
NOT_REGISTERED = (
    "Not registered. Reopen the confirmation if the directory or its authorization changed."
)


def _settings():
    fd = os.open(SETTINGS, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("private operator configuration required")
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("oversized operator configuration")
    settings = json.loads(raw)
    if (
        not isinstance(settings, dict)
        or settings.keys() != _SETTINGS_FIELDS
        or type(settings["format"]) is not int
        or settings["format"] != 1
        or any(
            not isinstance(value, str) or not value or not value.isprintable()
            for field, value in settings.items()
            if field != "format"
        )
    ):
        raise ValueError("invalid operator configuration")
    for key in ("bridge_python", "binding"):
        path = Path(settings[key])
        if path.anchor != "/" or ".." in path.parts or str(path) != settings[key]:
            raise ValueError("absolute normalized operator paths required")
    if not settings["chat_id"].startswith("local-"):
        raise ValueError("an existing local Bot Chat is required")
    return settings


def _worker(settings, operation, **params):
    request = {
        "operation": operation,
        "owner": {"bot_id": settings["profile"], "conversation_id": settings["session_id"]},
        **params,
    }
    result = subprocess.run(
        [
            settings["bridge_python"],
            "-I",
            "-m",
            "herdr_agent_bridge.hermes_registration",
            settings["binding"],
        ],
        input=json.dumps(request, ensure_ascii=True).encode("ascii"),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": os.defpath, "HOME": str(Path.home()), "LANG": "C.UTF-8"},
        timeout=12,
        check=True,
    )
    if len(result.stdout) > 65536:
        raise ValueError("oversized registration result")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("invalid registration result")
    return value


def _authorized(ctx, settings, platform, session_id, source):
    # Every identity is provided by Hermes, never by slash arguments or model text.
    return (
        platform == "local"
        and source is not None
        and ctx.profile_name == settings["profile"]
        and session_id == settings["session_id"]
        and getattr(source, "chat_type", None) == "dm"
        and getattr(source, "chat_id", None) == settings["chat_id"]
        and getattr(source, "user_id", None) == settings["user_id"]
    )


def register(ctx):
    pending = threading.Lock()

    def command(raw_args, *, session_id="", platform="", source=None):
        try:
            settings = _settings()
        except (OSError, ValueError, TypeError):
            return "Project registration is not configured for this Bot Chat."
        if not _authorized(ctx, settings, platform, session_id, source):
            return "Project registration is not authorized in this session."
        selector = raw_args.strip()
        if not selector or len(selector) > 4096 or not selector.isprintable():
            return "Usage: /hab-register PROJECT_NAME_OR_ABSOLUTE_DIRECTORY"
        if not pending.acquire(blocking=False):
            return "A project registration confirmation is already open in this Bot Chat."
        attempted = False
        try:
            result = _worker(settings, "prepare", selector=selector)
            status = result["status"]
            if status == "registered":
                entry = result["matches"][0]
                return f"Already registered: {entry['root']} ({entry['project_id']}). No confirmation needed."
            if status in {"ambiguous", "incomplete"}:
                candidates = (
                    json.dumps(item["root"], ensure_ascii=False) for item in result["matches"]
                )
                paths = "\n".join(candidates)
                suffix = "\nMore matches are not shown." if result["more"] else ""
                return (
                    f"Choose an exact directory and run /hab-register with that full path.\n{paths}{suffix}"
                ).strip()
            if status != "needs_confirmation":
                return "Not registered. No accessible project matched that selection."
            proposal = result["proposal"]
            exact = json.dumps(proposal["root"], ensure_ascii=False)
            question = f"Register this project directory?\n{exact}\nOnly registers the project identity. Does not dispatch a task or grant execution."
            expires = time.monotonic() + CONFIRM_SECONDS
            selected = ctx.ask_choice(
                question,
                [
                    {"label": "Register this directory", "value": "register"},
                    {"label": "Cancel", "value": "cancel"},
                ],
            )
            if (
                selected != "register"
                or time.monotonic() >= expires
                or _settings() != settings
                or not _authorized(ctx, settings, platform, session_id, source)
            ):
                return NOT_REGISTERED
            attempted = True
            done = _worker(settings, "commit", proposal=proposal)
            if done["status"] == "unknown":
                return (
                    "Registration outcome unavailable. Check the original project before retrying."
                )
            if done["status"] != "registered":
                return NOT_REGISTERED
            entry = done["entry"]
            return f"Registered: {entry['root']} ({entry['project_id']}). No execution permission or task was created."
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            # A lost reply is not proof that a write failed. Resolve the original directory to check it.
            if attempted:
                return (
                    "Registration outcome unavailable. Check the original project before retrying."
                )
            return NOT_REGISTERED
        finally:
            pending.release()

    ctx.register_command(
        "hab-register",
        command,
        description="Confirm the exact directory before registering a project",
        args_hint="PROJECT_NAME_OR_ABSOLUTE_DIRECTORY",
    )
