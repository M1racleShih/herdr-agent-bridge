"""Reject unexplained development-stage names in product paths and text."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "tools/naming_exceptions.json"
PATTERN = re.compile(
    r"phase[\s_-]*\d+|阶段\s*[一二三四五六七八九十\d]+|roadmap\s+\d+|(?<!\w)(?:hab-)?f[1-5]-",
    re.IGNORECASE,
)
SKIP_PARTS = {
    ".git",
    ".artifacts",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
}
TEXT_SUFFIXES = {".py", ".mjs", ".json", ".md", ".toml", ".yml", ".yaml", ".txt", ".in", ".sh"}


def files(root):
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        candidates = [root / name.decode() for name in result.stdout.split(b"\0") if name]
    else:
        # Source distributions have no Git metadata; inspect their shipped inputs.
        candidates = root.rglob("*")
    return sorted(
        {
            path
            for path in candidates
            if path.is_file()
            and not any(
                part in SKIP_PARTS or part.endswith(".egg-info")
                for part in path.relative_to(root).parts
            )
        }
    )


def scan(root, entries=None):
    root = Path(root)
    if entries is None:
        try:
            entries = json.loads((root / REGISTRY).read_text())
        except (OSError, ValueError):
            return ["cannot read historical naming exceptions"]
    if not isinstance(entries, list):
        return ["historical naming exceptions must be a list"]
    allowed, issues, seen = {}, [], set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "line", "reason"}
            or not all(
                isinstance(entry[key], str) and entry[key].strip()
                for key in ("path", "line", "reason")
            )
        ):
            issues.append("historical naming exception needs path, exact line and reason")
            continue
        key = (entry["path"], entry["line"])
        if key in allowed:
            issues.append("duplicate historical naming exception: " + entry["path"])
        allowed[key] = entry["reason"]
    for path in files(root):
        relative = path.relative_to(root).as_posix()
        if PATTERN.search(relative):
            issues.append(relative + ": development-stage filename")
        if relative == REGISTRY or path.suffix not in TEXT_SUFFIXES:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            key = (relative, line.strip())
            if key in allowed:
                seen.add(key)
            elif PATTERN.search(line):
                issues.append(f"{relative}:{number}: unexplained development-stage name")
    for path, line in allowed.keys() - seen:
        issues.append(path + ": stale historical naming exception: " + line)
    return sorted(issues)


def main():
    issues = scan(ROOT)
    for issue in issues:
        print(issue)
    if not issues:
        print("Product naming check passed; historical exceptions are exact and explained.")
    return bool(issues)


if __name__ == "__main__":
    raise SystemExit(main())
