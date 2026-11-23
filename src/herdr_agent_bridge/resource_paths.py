"""Persistent installed resource paths, independent of cwd and project state.

Wheel installers unpack resources alongside this package. A zip-imported package
is unsupported: Node child processes need the whole sibling resource directory
for their lifetime, including relative .mjs imports.
"""

from importlib.resources import files
from pathlib import Path


def resource_path(name: str) -> Path:
    path = Path(str(files("herdr_agent_bridge.resources").joinpath(name)))
    if not path.is_file():
        raise FileNotFoundError("installed bridge resource missing: " + name)
    return path
