"""Show the bridge terminal in an existing or isolated Herdr App window.

Native runs attach to an existing window; isolated runs use a dedicated window.
Uncertain binding steps must be inspected, never blindly replayed.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from herdr_agent_bridge.agent_bridge import Bridge
from herdr_agent_bridge.bridge_common import BridgeError, save

APP_TERMINAL_NAME = "herdr-bridge-app"
# Missing-field records identify the original isolated App terminal only.
LEGACY_APP_TERMINAL_NAME = "phase3-app"


class AppSession:
    def __init__(self, run):
        self.root = Path(run).resolve()
        self.bridge = Bridge(self.root / "bridge")
        self.path = self.root / "app.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def env(self):
        env = self.bridge.env()
        if self.bridge.data.get("runtime", "isolated") == "native":
            return env
        env.update(
            HERDR_APP_HOME=str(Path(self.bridge.data["scratch"]) / "app"), HERDR_APP_NO_SOUND="1"
        )
        if self.data.get("display"):
            env.update(DISPLAY=self.data["display"], WAYLAND_DISPLAY="")
        return env

    def command(self, *args):
        result = subprocess.run(
            ["herdr-app", *args], env=self.env(), capture_output=True, text=True, timeout=20
        )
        if result.returncode:
            raise BridgeError("App command failed; inspect app.json and original window")
        try:
            return json.loads(result.stdout)
        except ValueError:
            return result.stdout.strip()

    def start(self, display=None, title="Herdr Agent Bridge"):
        if self.data:
            raise BridgeError("App launch already attempted; inspect original App")
        if self.bridge.data.get("runtime", "isolated") == "native":
            if self.bridge.data.get("closed"):
                raise BridgeError("bridge session is closed")
            if display is not None:
                raise BridgeError("native App uses the existing window, not a display override")
            self._locations(self.command("ls"), None)
            self.data = {"runtime": "native", "launch": "attached", "binding": "not_attempted"}
            save(self.path, self.data)
            return self.data
        self.data = {
            "launch": "unknown",
            "display": display,
            "binding": "not_attempted",
            "title": title,
            "terminal_name": APP_TERMINAL_NAME,
        }
        save(self.path, self.data)
        env = self.env()
        overrides = ["HERDR_APP_HOME=" + env["HERDR_APP_HOME"], "HERDR_APP_NO_SOUND=1"]
        if display:
            overrides += ["DISPLAY=" + display, "WAYLAND_DISPLAY="]
        result = self.bridge.hc(
            "run",
            "--bg",
            "--json",
            "--no-parent",
            "--no-notify",
            "--name",
            APP_TERMINAL_NAME,
            "env",
            *overrides,
            "herdr-app",
        )
        self.data.update(launch="submitted", terminal=result["address"])
        save(self.path, self.data)
        return self.data

    @staticmethod
    def _locations(mapping, address):
        """Validate the listing and retain only coordinates for our terminal."""
        if not isinstance(mapping, dict) or not isinstance(mapping.get("spaces"), list):
            raise BridgeError("invalid App mapping; inspect the existing window")
        locations = []
        for space in mapping["spaces"]:
            if not isinstance(space, dict) or not isinstance(space.get("tabs"), list):
                raise BridgeError("invalid App mapping; inspect the existing window")
            for tab in space["tabs"]:
                if not isinstance(tab, dict) or not isinstance(tab.get("terminals"), list):
                    raise BridgeError("invalid App mapping; inspect the existing window")
                for terminal in tab["terminals"]:
                    if not isinstance(terminal, dict):
                        raise BridgeError("invalid App mapping; inspect the existing window")
                    if address is not None and terminal.get("terminal") == address:
                        if not all(
                            isinstance(value, str) and value
                            for value in (space.get("id"), tab.get("id"), terminal.get("pane"))
                        ):
                            raise BridgeError("owned App pane has incomplete coordinates")
                        locations.append(
                            {
                                "space": space["id"],
                                "tab": tab["id"],
                                "pane": terminal["pane"],
                                "terminal": address,
                            }
                        )
        return locations

    @staticmethod
    def _native_space(mapping, space_id):
        spaces = [space for space in mapping["spaces"] if space.get("id") == space_id]
        if len(spaces) != 1:
            raise BridgeError("requested App space is not uniquely present")
        if "machine" not in spaces[0] or spaces[0]["machine"] not in (None, ""):
            raise BridgeError("native bridge terminal requires a local App space")

    def _owned_terminal(self, address):
        terminal = self.bridge.hc("get", "--json", address)
        if not isinstance(terminal, dict) or terminal.get("address") != address:
            raise BridgeError("terminal address changed; refusing App binding")
        self.bridge.identity(terminal)
        if terminal.get("state") != "live":
            raise BridgeError("owned terminal is not live")
        if not isinstance(terminal.get("labels"), dict):
            raise BridgeError("owned terminal labels are unavailable")
        return terminal

    def _bind_native(self, space_id):
        if self.data.get("launch") != "attached" or self.data.get("closed"):
            raise BridgeError("native App association is not open")
        if self.bridge.data.get("closed"):
            raise BridgeError("bridge session is closed")
        if self.data.get("binding") != "not_attempted":
            raise BridgeError("binding already attempted; inspect App before recovery")
        if not isinstance(space_id, str) or not space_id:
            raise BridgeError("native App binding requires an explicit space id")
        address = self.bridge.data.get("address")
        if not isinstance(address, str) or not address:
            raise BridgeError("owned terminal address is unresolved")
        terminal = self._owned_terminal(address)
        labels = terminal["labels"]
        if labels.get("herdr.space") not in (None, space_id) or (
            labels.get("herdr.tab") and not labels.get("herdr.space")
        ):
            raise BridgeError("owned terminal already has conflicting App labels")
        mapping = self.command("ls")
        locations = self._locations(mapping, address)
        self._native_space(mapping, space_id)
        if any(location["space"] != space_id for location in locations) or len(locations) > 1:
            raise BridgeError("owned terminal is already mapped elsewhere or ambiguously")

        # The label may be applied even when the reply is lost. Record intent first.
        self.data.update(binding="unknown", space=space_id, address=address)
        save(self.path, self.data)
        if labels.get("herdr.space") != space_id:
            self.bridge.hc("label", address, "herdr.space=" + space_id)
            self.data["label"] = "submitted"
            save(self.path, self.data)

        deadline = time.monotonic() + 15
        while True:
            mapping = self.command("ls")
            locations = self._locations(mapping, address)
            self._native_space(mapping, space_id)
            if any(location["space"] != space_id for location in locations) or len(locations) > 1:
                raise BridgeError("owned terminal appeared outside the requested space")
            if locations:
                terminal = self._owned_terminal(address)
                if terminal["labels"].get("herdr.space") != space_id:
                    raise BridgeError("owned terminal App label changed during binding")
                self.data.update(binding="bound", visible=locations)
                save(self.path, self.data)
                return self.data
            if time.monotonic() >= deadline:
                raise BridgeError("App pane creation still unresolved; inspect original binding")
            time.sleep(0.5)

    def bind(self, space=None):
        if self.bridge.data.get("runtime", "isolated") == "native":
            return self._bind_native(space)
        if space is not None:
            raise BridgeError("explicit spaces are supported only for the native App")
        if self.data.get("binding") != "not_attempted":
            raise BridgeError("binding already attempted; inspect App before recovery")
        self.data["binding"] = "unknown"
        save(self.path, self.data)
        space = self.command(
            "do",
            "space.new",
            json.dumps(
                {
                    "name": self.data.get("title", "Herdr Agent Bridge"),
                    "tab": "Setup",
                    "folder": self.bridge.data["project"],
                }
            ),
        )
        self.data["space"] = space
        save(self.path, self.data)
        shown = self.command(
            "do",
            "terminal.keep",
            json.dumps({"terminal": self.bridge.data["address"], "place": "tab"}),
        )
        self.data["shown"] = shown
        save(self.path, self.data)
        deadline = time.monotonic() + 15
        while True:
            mapping = self.command("ls")
            visible = [
                terminal
                for space_item in mapping.get("spaces", [])
                for tab in space_item.get("tabs", [])
                for terminal in tab.get("terminals", [])
                if terminal.get("terminal") == self.bridge.data["address"]
            ]
            if visible:
                self.data["visible"] = visible
                save(self.path, self.data)
                break
            if time.monotonic() >= deadline:
                raise BridgeError("App pane creation still unresolved; inspect original binding")
            time.sleep(0.5)
        self.command("do", "tab.close", json.dumps({"id": space["tab"], "confirmed": True}))
        result = self.bridge.hc("wait", "--timeout", "10", space["terminal"])
        if not result.get("ended"):
            raise BridgeError("setup terminal close unresolved; inspect original binding")
        self.bridge.hc("rm", space["terminal"])
        self.command("do", "agent.focus", json.dumps({"terminal": self.bridge.data["address"]}))
        self.data.update(binding="bound", mapping=self.command("ls"), status=self.command("status"))
        save(self.path, self.data)
        return self.data

    def close(self):
        if self.bridge.data.get("runtime", "isolated") == "native":
            if self.data.get("launch") != "attached":
                raise BridgeError("native App association is not open")
            self.data["closed"] = True
            save(self.path, self.data)
            return self.data
        address = self.data.get("terminal")
        if not address:
            raise BridgeError("App launch identity unresolved")
        terminal = self.bridge.hc("get", "--json", address)
        expected_name = self.data.get("terminal_name", LEGACY_APP_TERMINAL_NAME)
        if (
            expected_name not in (APP_TERMINAL_NAME, LEGACY_APP_TERMINAL_NAME)
            or terminal.get("address") != address
            or terminal.get("name") != expected_name
            or "herdr-app" not in terminal.get("argv", [])
        ):
            raise BridgeError("App terminal identity changed")
        self.bridge.hc("kill", address)
        self.bridge.hc("rm", address)
        self.data["closed"] = True
        save(self.path, self.data)
        return self.data

    def status(self):
        if self.bridge.data.get("runtime", "isolated") != "native":
            return {"mapping": self.command("ls"), "status": self.command("status")}
        address = self.data.get("address") or self.bridge.data.get("address")
        if not address or self.data.get("closed"):
            return {"association": self.data, "visible": []}
        terminal = self._owned_terminal(address)
        visible = self._locations(self.command("ls"), address)
        if visible and self.bridge.native_codex():
            with self.bridge.locked():
                self.bridge.identity(terminal)
                selection = self.bridge.agent_selection(terminal=terminal)
                self.bridge.persist()
            if selection["state"] == "ready":
                visible = [
                    {**location, "thread_id": selection["session_id"]} for location in visible
                ]
        return {"association": self.data, "visible": visible}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("action", choices=("start", "bind", "status", "shot", "close"))
    parser.add_argument("--display")
    parser.add_argument("--title", default="Herdr Agent Bridge")
    parser.add_argument("--space", help="existing local App space id for native binding")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    app = AppSession(args.run)
    if args.action == "start":
        result = app.start(args.display, args.title)
    elif args.action == "bind":
        result = app.bind(args.space)
    elif args.action == "status":
        result = app.status()
    elif args.action == "shot":
        if not args.output:
            parser.error("--output required for screenshot")
        result = app.command("shot", str(args.output.resolve()))
    else:
        result = app.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
