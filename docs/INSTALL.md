# Install and first run

[简体中文](INSTALL.zh-CN.md) · [README](../README.md) · [Operating guide](PROJECT.md)

Install the **required runtime** [herdr-core](https://github.com/herdrdev/herdr-core#install) separately on the host running Bridge and the coding sessions. Bridge invokes its `hc` executable through `PATH` to control terminals. Linux integrations were tested with `hc 0.1.22`. Bridge itself needs Python 3.11+ and uses only the Python standard library at runtime. The no-model smoke supports Linux and macOS. See the [support matrix](../README.md#compatibility).

For Pi development, also install [Node.js](https://nodejs.org/en/download) and [Pi](https://github.com/earendil-works/pi/tree/main/packages/coding-agent); for Codex development, install the [Codex CLI](https://github.com/openai/codex). Both need working model access and native authentication. None is needed for the no-model smoke.

## Ask an agent to install

Copy this instruction to an agent with access to your machine:

> Install herdr-agent-bridge 0.1.0 by following https://github.com/M1racleShih/herdr-agent-bridge/blob/main/docs/INSTALL.md. Check existing installations first and run the no-model smoke. Ask before logging in, incurring model costs, changing global settings or skills, or using an existing session. Do not start a model task without my authorization.

The commands below install version 0.1.0. Installing the bundled agent skill is optional.

## Check the environment

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed. uv can obtain Python 3.11. Check your tools first.

```sh
uv --version
hc --version
command -v herdr-bridge || true
uv tool list
```

If `hc` is missing, the upstream Linux and macOS installer is:

```sh
curl -fsSL https://releases.herdr.dev/core/install.sh | sh
hc --version
```

If `herdr-bridge` already exists, check `herdr-bridge --version` and its location. Reconcile active runs before replacing an installation: their background observers use the original Python environment until cleanup. Avoid overwriting a different installation without deciding how to preserve it.

## Install version 0.1.0

Use uv to install version 0.1.0 from [PyPI](https://pypi.org/project/herdr-agent-bridge/0.1.0/).

```sh
uv tool install --python 3.11 'herdr-agent-bridge==0.1.0'
export PATH="$(uv tool dir --bin):$PATH"
herdr-bridge --version
herdr-bridge --help
```

The version command should print `herdr-bridge 0.1.0`. If the executable is not found, add the directory printed by `uv tool dir --bin` to your current `PATH`, then retry; `uv tool update-shell` changes shell configuration and should be used only when authorized. Alternatively, install a verified 0.1.0 wheel with `uv tool install --python 3.11 /path/to/herdr_agent_bridge-0.1.0-py3-none-any.whl`.

## First success without a model

Create a dedicated scratch directory and run from it; the report defaults to `.artifacts/hc-smoke/results.json` relative to that directory:

```sh
HAB_CALLER_DIR="$PWD"
HAB_SMOKE_DIR="$(mktemp -d)"
cd "$HAB_SMOKE_DIR"
herdr-bridge tool smoke_hc
```

The command uses the installed `hc`, a random dedicated session, isolated XDG directories and an inert local fixture. It does not call a model or external service. Success is exit code 0 and JSON with `"passed": true`, three passed checks and the report path. It removes its dedicated terminal, server and scratch directory after successful cleanup; the report remains. This establishes an hc baseline, not a real coding-agent or integration acceptance.

If it fails or is interrupted, inspect the report's `error`, `session`, `scratch` and `cleanup` fields before taking action. Reconcile and clean only the session and scratch paths identified by that report; do not stop unrelated sessions or blindly repeat an uncertain operation. Keep the report private if it contains machine paths. After a successful run, if the report is no longer needed, remove the dedicated first-run directory in the same shell:

```sh
cd "$HAB_CALLER_DIR"
rm "$HAB_SMOKE_DIR/.artifacts/hc-smoke/results.json"
rmdir "$HAB_SMOKE_DIR/.artifacts/hc-smoke" "$HAB_SMOKE_DIR/.artifacts" "$HAB_SMOKE_DIR"
```

To remove a uv tool installation after all of its runs and observers are closed, use `uv tool uninstall herdr-agent-bridge`. This does not remove hc, project configuration, journals or any skill copied separately.

## Optional: a real task

Only after explicit authorization for the project, agent, model access and potential costs, follow the [project guide](PROJECT.md#start-continue-and-close) to create a private project binding and a small development contract, start the chosen native Pi or Codex agent, submit one task, and independently verify its result. Handle permission and trust prompts as the human operator. Finish the task with an explicit decision and close the dedicated run; preserve journals and evidence needed for recovery. A timeout or lost reply means delivery is unknown until the original request is reconciled, so do not create or resend a task blindly. For isolated model-backed acceptance and its cleanup, see [Contributing](../CONTRIBUTING.md).

## CLI and upgrade contract

Fixed projects use `herdr-bridge --config /private/project.json COMMAND`; multi-project bindings use the [personal agent entry](PROJECT.md#personal-agent-entry). Consult `herdr-bridge --help` and each subcommand's `--help`. `env -u PYTHONPATH -u PYTHONHOME python -P -m herdr_agent_bridge` is the same entry when run with the installed Python environment. Lower-level interfaces are `herdr-bridge manager`, `herdr-bridge agent` and `herdr-bridge app`; `herdr-bridge tool NAME` selects a development or acceptance program. Acceptance programs read their stated source inputs from the current directory, so run them in a dedicated acceptance project containing those inputs. Normal project operations need neither the source checkout nor a harness.

Successful project commands emit one JSON document on stdout. Operational errors exit 1 with safe diagnostic JSON on stderr; argument errors exit 2 with argparse text on stderr. Independent verification failure exits 1. Version and help are text with exit 0. Lower-level compatibility commands retain their own output and exit contracts. Start, submit, recover and uncertain notification delivery are not retried automatically; request, round, native session, event identities and reason codes retain their meanings.

Wheel and sdist include the MJS and protocol JSON resources. A normal wheel installation unpacks them for Node and its adjacent imports; direct Node resource execution from a zip is unsupported. Background observers run using the current interpreter via `-P -m herdr_agent_bridge.managed_watch` or `-P -m herdr_agent_bridge.grok_bridge`. Keep that installed environment available until the run's observers and agent are cleaned up. These subprocesses clear `PYTHONPATH` and `PYTHONHOME`, prevent current-directory shadowing and retain the installed environment and user site, including an existing `PYTHONUSERBASE`.

Existing configuration, journals, outboxes and task pointers are read in place; missing new fields keep their existing defaults. Migration does not recreate requests. Source `scripts/*.py` remain thin entry points and require the package installed in the same Python environment; responsibility-named scripts and the `project_entry.run_bound` import remain supported. Adjacent MJS/JSON files are private package resources, not public script APIs. New fixed wrappers should import `herdr_agent_bridge.project_entry.run_bound` and retain the original private configuration. Do not add `src` to `PYTHONPATH` or recreate a task to change the launch command.

For old source-verification rounds, the persisted original prompt selects the old or new source and verification checks the original path's actual text and AST. An answer cannot switch sources. Historical reports remain readable, but verification succeeds only when the referenced original source still matches; do not substitute installed package contents for a changed or thin-wrapper historical path.

<a id="runtime-and-acceptance-directories"></a>
## Runtime and acceptance directories

New Manager runs use `.artifacts/runs/<run-id>/` in the project. The fixed project entry creates this pointer; lower-level `manager start --run` accepts only a new dedicated run beneath this directory. Project binding names remain a single safe path component; notifications must match the original project, owner, run and event identities.

Acceptance records use `.artifacts/acceptance/codex/<id>/`, `.artifacts/acceptance/pi/<id>/` and `.artifacts/acceptance/core-features/<id>/`. Each contains `acceptance.json` and relevant call logs and snapshots. Pi and core-feature reports associate a separate Manager directory by `run`; `accept_pi --run` identifies the Manager run, and `accept_codex --output` can select a new evidence directory.

Existing `.artifacts/phase2/` runs, configurations, journals and outboxes remain at their original paths;
new runs cannot be created there. Reading them does not migrate records or recreate requests. Recovery, continuation and close use the original identities.
Historical `.artifacts/phase1/` and `.artifacts/phase5/` acceptance reports remain in place.
Read them at their original paths; the current Codex renderer can render an old Codex report without rewriting the evidence.

New isolated App terminals use `herdr-bridge-app`, saved in `app.json` before launch. Old records without a name field use
the original `phase3-app` terminal name. Cleanup also matches the saved terminal address and App
launch arguments. The native App retains its same-window association and takeover contract.

The public Codex acceptance commands are `herdr-bridge tool accept_codex` and `herdr-bridge tool render_codex_report`. The optional renderer produces Chinese-language HTML. Removed development-stage modules, scripts and commands have no public aliases; update callers without changing the original run, request or event identities.

## Optional product skill

The bundled `skills/herdr-agent-bridge/` applies to CLI 0.1.x. You may copy that complete directory from a source checkout, or locate it using the Python interpreter of the installed distribution:

```sh
/path/to/installed/python -c 'from importlib.metadata import distribution; d=distribution("herdr-agent-bridge"); print(next(d.locate_file(p).resolve().parent for p in d.files if str(p).endswith("share/herdr-agent-bridge/skills/herdr-agent-bridge/SKILL.md")))'
```

Copy the complete directory to the target agent's supported skill location only with authorization, and refresh discovery as required by that host. Check installation, discovery and actual Bot/tool delivery separately. The skill grants no additional access, permission approval, commit or deployment authority.
