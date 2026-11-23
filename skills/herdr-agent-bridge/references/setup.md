# Setup and discovery

The product ships this skill with the matching CLI release. Install the wheel in
a Python 3.11+ environment, then copy the whole skill folder into the intended
Agent's supported skill-discovery directory. Do not replace unrelated skills or
harness bindings. Source `skills/herdr-agent-bridge/` and the wheel's
`share/herdr-agent-bridge/skills/herdr-agent-bridge/` are equivalent copies.
Locate the installed folder through the selected environment’s distribution
metadata; this also handles `--user` and custom user bases:

```sh
python -c 'from importlib.metadata import distribution; d=distribution("herdr-agent-bridge"); print(next(d.locate_file(p).resolve().parent for p in d.files if str(p).endswith("share/herdr-agent-bridge/skills/herdr-agent-bridge/SKILL.md")))'
```

Run that command with the same Python environment used for the installed CLI. Default sysconfig data paths alone do not locate user
installs. Restart or refresh that Agent's skills as its host requires; copying a
folder does not prove the host discovered it. This skill supports CLI 0.1.x.

Verify the trusted installed executable with `herdr-bridge --version` and
`herdr-bridge --help`. Keep that environment available while its background
observers or Agent sessions are active. Python runtime dependencies are standard
library only. Install hc, the selected agent's CLI and its authorized native
authentication. Pi also needs Node.js. Both native agents keep their normal
user and project resources and permissions.
Read-only prompts are not an OS sandbox.

For a Personal Agent managing multiple projects, use the single fixed wrapper
provided by the operator. It binds the catalog, authorized roots, owner and
execution config mapping. Run its `resolve NAME_OR_ID` first (`discover` lists
visible Git candidates and scan coverage). For `ambiguous`, show the matches and
ask which project; for `incomplete`, do not assume a unique match. For
`needs_confirmation`, show the exact directory. If the operator enabled the
Hermes Desktop registration plugin, ask the user to type `/hab-register EXACT_DIRECTORY`
and confirm the displayed directory on Hermes's confirmation card. Do not run
this slash command yourself, present a free-text reply as consent, or dispatch
before registration and execution binding. Resolve again after confirmation.
If no trusted confirmation handler is installed, hand the directory to the local operator for
`herdr-bridge personal --binding /private/caller.json register DIRECTORY`.
The Agent's fixed wrapper cannot register, replace a binding or add execution
permissions. `project_not_bound` means the operator still needs to set up the
project's execution config; never fall back to raw shell or an unbound CLI.

For a single project, use its existing private config or fixed wrapper. Inspect
`config` and `status` before creating anything. For a new authorized binding:

```sh
herdr-bridge --config /private/project.json init --project-id PROJECT_ID --project /project/root --bot-id BOT_ID --conversation-id CONVERSATION_ID --checks-file /private/checks.json
herdr-bridge --config /private/project.json config
```

Choose identifiers and paths from the authorized project. The CLI creates the
private config; do not put credentials into prompts or this skill. Named checks
are argv arrays with bounded timeouts and may execute project code: review scope
before registering them. New projects default to native Pi. Use `init --agent codex`
to select Codex for a new project. Configure a webhook
only when the destination and credential are authorized. Source docs INSTALL.md,
PROJECT.md and PROJECT_BOT_TEMPLATE.md contain enrollment details; the installed
CLI help is the parameter authority.
