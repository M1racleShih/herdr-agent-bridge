"""Local project selection; registration and task dispatch are separate operations."""

from __future__ import annotations

import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from herdr_agent_bridge.bridge_common import BridgeError
from herdr_agent_bridge.project_bridge import Project, identifier, private_json

MAX_DEPTH = 4
MAX_CANDIDATES = 100
MAX_ENTRIES = 10_000
REGISTRY_LIMIT = 4_194_304  # Same byte limit as private_json.
IGNORED_DIRS = frozenset({"build", "dist", "node_modules", "vendor", "venv", "__pycache__"})
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _absolute_directory(value):
    path = Path(value)
    if path.anchor != "/" or ".." in path.parts or not str(path).isprintable():
        raise BridgeError("project directories must be absolute paths without traversal")
    return path


def _name(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 120
        or not value.isprintable()
    ):
        raise BridgeError("project names must contain 1–120 printable characters")
    return value.strip()


@contextmanager
def _open_directory(path):
    """Walk from the filesystem root without following links at any component."""
    fd = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _directory_identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _check_directory_identity(fd, expected):
    if expected is not None and _directory_identity(fd) != expected:
        raise BridgeError("project directory changed since confirmation was requested")


def _git_marker(fd):
    try:
        mode = os.stat(".git", dir_fd=fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(mode):
        return "git_directory"
    if stat.S_ISREG(mode):
        return "git_file"
    return None


class ProjectRegistry:
    """Shared private catalog with an authorization scope supplied by a trusted caller."""

    def __init__(self, store, *, allowed_roots):
        self.path = Path(store).absolute()
        self.allowed_roots = tuple(sorted({_absolute_directory(root) for root in allowed_roots}))

    def _authorized(self, path):
        return any(path.is_relative_to(allowed) for allowed in self.allowed_roots)

    def _entries(self):
        try:
            self.path.lstat()
        except FileNotFoundError:
            return []
        saved = private_json(self.path)
        if (
            not isinstance(saved, dict)
            or saved.keys() != {"format", "projects"}
            or type(saved["format"]) is not int
            or saved["format"] != 1
            or not isinstance(saved["projects"], list)
        ):
            raise BridgeError("invalid project registry")
        ids, roots = set(), set()
        for entry in saved["projects"]:
            if (
                not isinstance(entry, dict)
                or entry.keys() != {"project_id", "root", "name"}
                or not isinstance(entry["root"], str)
            ):
                raise BridgeError("invalid project registry entry")
            path = _absolute_directory(entry["root"])
            project_id = identifier(entry["project_id"])
            if (
                str(path) != entry["root"]
                or _name(entry["name"]) != entry["name"]
                or project_id in ids
                or path in roots
            ):
                raise BridgeError("invalid or duplicate project registry entry")
            roots.add(path)
            ids.add(project_id)
        return saved["projects"]

    def prepare_registration(self, selector):
        """Return one read-only directory proposal, or a lookup verdict without a proposal."""
        if not isinstance(selector, str) or not selector or len(selector) > 4096:
            raise BridgeError("invalid project selection")
        if selector.startswith("/"):
            path = _absolute_directory(selector)
            if str(path) != selector or not self._authorized(path):
                raise BridgeError("project is outside authorized roots")
            matches = [entry for entry in self._entries() if entry["root"] == selector]
            if matches and self._available(matches[0]):
                return {"status": "registered", "matches": matches}
        else:
            result = self.resolve(selector)
            if result["status"] != "needs_confirmation":
                return result
            path = Path(result["matches"][0]["root"])
        with _open_directory(path) as fd:
            return {
                "status": "needs_confirmation",
                "root": str(path),
                "directory_identity": list(_directory_identity(fd)),
            }

    def register_confirmed(self, root, *, name=None, expected_directory=None):
        """Register an exact directory chosen by a trusted operator after confirmation.

        This method is not available through the bound Bot entry. It provisions
        a project identity only; it does not authorize or start any Agent.
        """
        path = _absolute_directory(root)
        if not self._authorized(path):
            raise BridgeError("project directory is outside authorized roots")
        name = _name(path.name if name is None else name)
        # Validate before creating storage and again under its write lock.
        try:
            with _open_directory(path) as fd:
                _check_directory_identity(fd, expected_directory)
        except OSError:
            raise BridgeError(
                "project directory must be accessible and contain no symlinks"
            ) from None
        self.path.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
        with Project.file_lock(self.path), _open_directory(path) as fd:
            _check_directory_identity(fd, expected_directory)
            entries = self._entries()
            for entry in entries:
                if entry["root"] == str(path):
                    return entry
            entry = {
                "project_id": "project-" + secrets.token_hex(12),
                "root": str(path),
                "name": name,
            }
            saved = {"format": 1, "projects": [*entries, entry]}
            if len(json.dumps(saved, ensure_ascii=False, indent=2).encode()) + 1 > REGISTRY_LIMIT:
                raise BridgeError("project registry exceeds its storage limit")
            Project.persist(self.path, saved)
            return entry

    def discover(
        self, *, max_depth=MAX_DEPTH, max_candidates=MAX_CANDIDATES, max_entries=MAX_ENTRIES
    ):
        """Read directory markers only, with explicit scan limits and coverage."""
        return self._discover(self._entries(), max_depth, max_candidates, max_entries)

    def _discover(self, entries, max_depth, max_candidates, max_entries):
        if any(
            type(value) is not int or not minimum <= value <= maximum
            for value, minimum, maximum in (
                (max_depth, 0, MAX_DEPTH),
                (max_candidates, 1, MAX_CANDIDATES),
                (max_entries, 1, MAX_ENTRIES),
            )
        ):
            raise BridgeError("invalid project discovery limits")
        registered = {entry["root"]: entry for entry in entries}
        candidates, seen, limited = [], set(), set()
        scanned_entries = 0

        def visit(fd, path, depth):
            nonlocal scanned_entries
            if path in seen:
                return
            seen.add(path)
            marker = _git_marker(fd)
            if marker:
                entry = registered.get(str(path))
                if entry is None and (
                    not path.name or len(path.name) > 120 or not path.name.strip()
                ):
                    return
                if len(candidates) == max_candidates:
                    limited.add("candidate_limit")
                    return
                candidates.append(
                    {
                        "root": str(path),
                        "name": entry["name"] if entry else path.name.strip(),
                        "project_id": entry["project_id"] if entry else None,
                        "marker": marker,
                    }
                )
                return
            if depth == max_depth:
                return
            with os.scandir(fd) as children:
                for child in children:
                    if scanned_entries == max_entries:
                        limited.add("entry_limit")
                        return
                    scanned_entries += 1
                    if (
                        child.name.startswith(".")
                        or child.name.casefold() in IGNORED_DIRS
                        or not child.name.isprintable()
                    ):
                        continue
                    try:
                        if child.is_dir(follow_symlinks=False):
                            child_fd = os.open(child.name, DIRECTORY_FLAGS, dir_fd=fd)
                            try:
                                visit(child_fd, path / child.name, depth + 1)
                            finally:
                                os.close(child_fd)
                    except OSError:
                        limited.add("unavailable_directory")
                    if limited & {"candidate_limit", "entry_limit"}:
                        return

        for path in self.allowed_roots:
            if limited & {"candidate_limit", "entry_limit"}:
                break
            try:
                with _open_directory(path) as fd:
                    visit(fd, path, 0)
            except OSError:
                limited.add("unavailable_directory")
        return {
            "candidates": sorted(candidates, key=lambda item: item["root"]),
            "coverage": {
                "roots": [str(path) for path in self.allowed_roots],
                "max_depth": max_depth,
                "max_candidates": max_candidates,
                "max_entries": max_entries,
                "examined_entries": scanned_entries,
                "limited_by": sorted(limited),
            },
        }

    def _available(self, entry):
        path = Path(entry["root"])
        if not self._authorized(path):
            return False
        try:
            with _open_directory(path):
                return True
        except OSError:
            return False

    def require_project(self, project_id):
        """Require a registered, currently authorized ID without running discovery."""
        identifier(project_id)
        for entry in self._entries():
            if entry["project_id"] == project_id and self._available(entry):
                return entry
        raise BridgeError(
            "project is not registered and accessible in this binding",
            code="project_not_authorized",
        )

    def resolve(self, selector):
        """Check authorized registrations first, then return read-only candidates for selection."""
        selector = _name(selector)
        entries = self._entries()
        matches = [
            entry
            for entry in entries
            if (selector == entry["project_id"] or selector.casefold() == entry["name"].casefold())
            and self._available(entry)
        ]
        exact = [entry for entry in matches if entry["project_id"] == selector]
        if exact or matches:
            matches = exact or matches
            return {
                "status": "registered" if len(matches) == 1 else "ambiguous",
                "matches": matches,
            }
        found = self._discover(entries, MAX_DEPTH, MAX_CANDIDATES, MAX_ENTRIES)
        matches = [
            item
            for item in found["candidates"]
            if item["name"].casefold() == selector.casefold() and item["project_id"] is None
        ]
        if len(matches) > 1:
            status = "ambiguous"
        elif found["coverage"]["limited_by"]:
            status = "incomplete"
        else:
            status = "needs_confirmation" if matches else "not_found"
        return {"status": status, "matches": matches, "coverage": found["coverage"]}
