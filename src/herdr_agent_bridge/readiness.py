"""Isolated environment and source readiness checks; no sessions or model calls."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_VERSION_TIMEOUT = 15
_VERSION_LIMIT = 160


def _regular_file(project, relative):
    """Return a non-symlink file below the given project, or None on failure."""
    try:
        root = Path(project)
        if root.is_symlink() or not root.is_dir():
            return None
        path = root
        for part in relative.split("/"):
            if part in ("", ".", ".."):
                return None
            path = path / part
            if path.is_symlink():
                return None
        return path if path.is_file() else None
    except (OSError, TypeError, ValueError):
        return None


def _version_line(output):
    if not isinstance(output, str):
        return ""
    # Only a bounded, printable first line is returned, never stderr or a traceback.
    first = output[:1024].splitlines()
    line = first[0] if first else ""
    return "".join(c if " " <= c <= "~" else "?" for c in line).strip()[:_VERSION_LIMIT]


def probe(project, runner=subprocess.run, agent="codex"):
    """Return local prerequisites, without starting a session or reading credentials.

    A temporary home and XDG paths keep version commands away from normal user
    configuration. A successful probe is not evidence of any integration.
    """
    from .readonly_contracts import REQUIRED_FILES

    if agent not in ("codex", "pi"):
        raise ValueError("unsupported coding agent")
    try:
        root = Path(project)
        project_exists = root.is_dir() and not root.is_symlink()
    except (OSError, TypeError, ValueError):
        project_exists = False
    checks = [
        {
            "check": "supported_platform",
            "passed": os.name == "posix" and sys.platform in ("linux", "darwin"),
            "platform": sys.platform,
        },
        {
            "check": "python_3_11_or_newer",
            "passed": sys.version_info >= (3, 11),
            "version": ".".join(str(part) for part in sys.version_info[:3]),
        },
        {"check": "project_directory", "passed": bool(project_exists)},
    ]
    checks.extend(
        {"check": "required_file:" + name, "passed": _regular_file(project, name) is not None}
        for name in REQUIRED_FILES
    )
    version_checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="hab-readiness-probe-") as directory:
            # Only PATH is inherited. The commands have no user's home, auth,
            # project cwd, or hc session/configuration in their environment.
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": directory,
                "XDG_CONFIG_HOME": str(Path(directory) / "config"),
                "XDG_STATE_HOME": str(Path(directory) / "state"),
                "XDG_CACHE_HOME": str(Path(directory) / "cache"),
                "XDG_RUNTIME_DIR": str(Path(directory) / "run"),
                "CODEX_HOME": str(Path(directory) / "codex"),
                "PI_CODING_AGENT_DIR": str(Path(directory) / "pi"),
                "PI_CODING_AGENT_SESSION_DIR": str(Path(directory) / "pi" / "sessions"),
                "PI_OFFLINE": "1",
                "PI_SKIP_VERSION_CHECK": "1",
                "PI_TELEMETRY": "0",
                "LC_ALL": "C",
                "NO_COLOR": "1",
            }
            for command in ("hc", agent):
                record = {"check": command + "_version", "passed": False}
                try:
                    result = runner(
                        [command, "--version"],
                        cwd=directory,
                        env=env.copy(),
                        capture_output=True,
                        text=True,
                        timeout=_VERSION_TIMEOUT,
                        check=False,
                    )
                    code = result.returncode
                    if type(code) is not int or not -255 <= code <= 255:
                        record["error"] = "invalid_returncode"
                    else:
                        record["returncode"] = code
                        version = _version_line(result.stdout)
                        if code == 0 and version:
                            record["version"] = version
                        record["passed"] = code == 0 and bool(version)
                except subprocess.TimeoutExpired:
                    record["error"] = "timeout"
                except FileNotFoundError:
                    record["error"] = "not_found"
                except OSError:
                    record["error"] = "os_error"
                except Exception:
                    record["error"] = "unexpected_error"
                version_checks.append(record)
    except Exception:
        # Do not include exception text, which can contain private local paths.
        version_checks = [
            {
                "check": command + "_version",
                "passed": False,
                "error": "isolated_environment_unavailable",
            }
            for command in ("hc", agent)
        ]
    checks.extend(version_checks)
    return {
        "ready": all(item["passed"] for item in checks),
        "agent": agent,
        "checks": checks,
        "not_verified": [
            "real Grok execution",
            "Grok login",
            "network access",
            "coding agent authentication",
        ],
    }
