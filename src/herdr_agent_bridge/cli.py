"""Installed public CLI. Project operations retain their existing JSON contract."""

import argparse
import importlib
import sys

from . import __version__
from .project_bridge import main as project_main


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print("herdr-bridge " + __version__)
        return 0
    if args and args[0] == "personal":
        from .personal_entry import main as personal_main

        return personal_main(args[1:])
    if args and args[0] in ("agent", "manager", "app", "tool"):
        namespace = args.pop(0)
        if namespace == "tool":
            parser = argparse.ArgumentParser(prog="herdr-bridge tool")
            parser.add_argument(
                "name",
                choices=(
                    "smoke_hc",
                    "accept_codex",
                    "accept_pi",
                    "accept_core_features",
                    "render_codex_report",
                ),
            )
            selected = parser.parse_args(args[:1])
            remaining = args[1:]
            module = "tools." + selected.name
            args = remaining
        else:
            module = {"agent": "agent_bridge", "manager": "grok_bridge", "app": "app_session"}[
                namespace
            ]
        return importlib.import_module("herdr_agent_bridge." + module).main(args)
    return project_main(args)
