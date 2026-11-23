"""Build both distributions and exercise clean installs outside the checkout.

Run with uv and the locked development environment. The installed regression
tests use local fixtures and do not call models or real hc. Build isolation is
disabled so builds use the backend versions from the lock.
"""

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path

from check_naming import PATTERN, scan

ROOT = Path(__file__).resolve().parents[1]


def run(argv, *, cwd, env=None):
    print("+", " ".join(map(str, argv)), flush=True)
    subprocess.run(list(map(str, argv)), cwd=cwd, env=env, check=True)


def require(value, message):
    if not value:
        raise RuntimeError(message)


def build(*options, cwd, out_dir, env):
    run(
        [
            "uv",
            "build",
            "--offline",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "-o",
            out_dir,
            *options,
        ],
        cwd=cwd,
        env=env,
    )


def main():
    require(not scan(ROOT), "source naming policy failed")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    with tempfile.TemporaryDirectory(prefix="hab-install-") as temporary:
        scratch = Path(temporary)
        dist = scratch / "dist"
        build("--sdist", "--wheel", cwd=ROOT, out_dir=dist, env=env)
        wheel = next(dist.glob("*.whl"))
        sdist = next(dist.glob("*.tar.gz"))
        skill = ROOT / "skills/herdr-agent-bridge"
        skill_files = sorted(skill.rglob("*.md"))
        require(len(skill_files) == 6, "skill entry and five references required")
        for path in skill_files:
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text()):
                linked = (path.parent / target).resolve()
                require(
                    linked.is_relative_to(skill.resolve()) and linked.is_file(),
                    "skill link must remain self-contained: " + target,
                )
        with zipfile.ZipFile(wheel) as archive:
            require(
                not any(PATTERN.search(name) for name in archive.namelist()),
                "wheel contains development-stage filenames",
            )
            for path in skill_files:
                suffix = "/share/herdr-agent-bridge/skills/herdr-agent-bridge/" + str(
                    path.relative_to(skill)
                )
                packaged = [name for name in archive.namelist() if name.endswith(suffix)]
                require(
                    len(packaged) == 1 and archive.read(packaged[0]) == path.read_bytes(),
                    "wheel skill content mismatch",
                )
            expected = {
                name
                for name in archive.namelist()
                if "/resources/" in name and name.endswith((".mjs", ".json", ".yaml"))
            }
            require(
                {name.split("/resources/", 1)[1] for name in expected}
                == {
                    "bridge_protocol.json",
                    "bridge_protocol.mjs",
                    "hermes_project_registration/plugin.yaml",
                    "pi_development_tools.mjs",
                    "pi_native_observer.mjs",
                    "pi_network.mjs",
                    "pi_oauth_session.mjs",
                },
                "wheel must contain all MJS, protocol JSON and Hermes registration resources",
            )
            for name in expected:
                require(
                    archive.read(name) == (ROOT / "src" / name).read_bytes(),
                    "wheel resource content mismatch: " + name,
                )
        with tarfile.open(sdist) as archive:
            members = archive.getnames()
            require(
                not any(PATTERN.search(name) for name in members),
                "sdist contains development-stage filenames",
            )
            for path in skill_files:
                suffix = "/skills/herdr-agent-bridge/" + str(path.relative_to(skill))
                packaged = [name for name in members if name.endswith(suffix)]
                require(
                    len(packaged) == 1
                    and archive.extractfile(packaged[0]).read() == path.read_bytes(),
                    "sdist skill content mismatch",
                )
            for name in expected:
                resource = [member for member in members if member.endswith("/src/" + name)]
                require(
                    len(resource) == 1
                    and archive.extractfile(resource[0]).read()
                    == (ROOT / "src" / name).read_bytes(),
                    "sdist resource content mismatch: " + name,
                )
            # A locally built archive; reject traversal before extraction.
            require(
                all(
                    not Path(name).is_absolute() and ".." not in Path(name).parts
                    for name in members
                ),
                "unsafe local sdist path",
            )
            require(
                all(member.isfile() or member.isdir() for member in archive.getmembers()),
                "local sdist must contain only regular files and directories",
            )
            if hasattr(tarfile, "data_filter"):
                archive.extractall(scratch / "source", filter="data")
            else:
                archive.extractall(scratch / "source")
        source = next((scratch / "source").iterdir())
        require(not scan(source), "sdist naming policy failed")
        build("--wheel", cwd=source, out_dir=scratch / "rebuilt", env=env)
        rebuilt = next((scratch / "rebuilt").glob("*.whl"))
        for index, artifact in enumerate((wheel, rebuilt)):
            home = scratch / ("env" + str(index))
            venv.EnvBuilder().create(home)
            python = home / "bin/python"
            cli = home / "bin/herdr-bridge"
            outside = scratch / ("outside" + str(index))
            outside.mkdir()
            run(
                ["uv", "pip", "install", "--offline", "--no-deps", "--python", python, artifact],
                cwd=outside,
                env=env,
            )
            run([cli, "--version"], cwd=outside, env=env)
            installed_skill = Path(
                subprocess.check_output(
                    [
                        python,
                        "-c",
                        'import sysconfig; print(sysconfig.get_path("data") + "/share/herdr-agent-bridge/skills/herdr-agent-bridge")',
                    ],
                    cwd=outside,
                    env=env,
                    text=True,
                ).strip()
            )
            for path in skill_files:
                require(
                    (installed_skill / path.relative_to(skill)).read_bytes() == path.read_bytes(),
                    "installed skill content mismatch",
                )
            run([cli, "--help"], cwd=outside, env=env)
            code = (
                "import json; from pathlib import Path; import herdr_agent_bridge as b; from herdr_agent_bridge.resource_paths import resource_path; from herdr_agent_bridge.bridge_protocol import AGENTS; assert not Path(b.__file__).is_relative_to(Path("
                + repr(str(ROOT))
                + ')); print(json.dumps({"package": b.__file__, "agents": AGENTS, "resource": str(resource_path("bridge_protocol.json"))}))'
            )
            run([python, "-c", code], cwd=outside, env=env)
            run(
                [
                    python,
                    "-B",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    (ROOT if index == 0 else source) / "tests",
                    "-v",
                ],
                cwd=outside,
                env=env,
            )
        # User-site installs must remain importable by the safe-path watcher.
        for custom_base in (True, False):
            user_python_home = scratch / ("user-python-" + str(custom_base))
            venv.EnvBuilder(with_pip=True, system_site_packages=True).create(user_python_home)
            user_env = {
                **env,
                "HOME": str(scratch / "home"),
                "PYTHONUSERBASE": os.path.relpath(scratch / "userbase", outside),
            }
            if not custom_base:
                user_env.pop("PYTHONUSERBASE", None)
                user_env["HOME"] = str(scratch / "default-user-home")
            base_python = str(user_python_home / "bin/python")
            run(
                [base_python, "-m", "pip", "install", "--user", "--no-index", "--no-deps", wheel],
                cwd=outside,
                env=user_env,
            )
            location = subprocess.check_output(
                [
                    base_python,
                    "-P",
                    "-c",
                    'from importlib.metadata import distribution; d=distribution("herdr-agent-bridge"); print(next(d.locate_file(p).resolve().parent for p in d.files if str(p).endswith("share/herdr-agent-bridge/skills/herdr-agent-bridge/SKILL.md")))',
                ],
                cwd=outside,
                env=user_env,
                text=True,
            ).strip()
            for path in skill_files:
                require(
                    (Path(location) / path.relative_to(skill)).read_bytes() == path.read_bytes(),
                    "user-install skill discovery mismatch",
                )
            run(
                [
                    base_python,
                    "-P",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    ROOT / "tests",
                    "-p",
                    "test_native_monitor.py",
                    "-v",
                ],
                cwd=outside,
                env=user_env,
            )
        print(
            json.dumps(
                {
                    "passed": True,
                    "clean_installs": 2,
                    "user_site_watcher": True,
                    "regression_each": True,
                    "temporary_resources_removed_on_exit": True,
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
