# Contributing

[简体中文](CONTRIBUTING.zh-CN.md) · [Install and first run](docs/INSTALL.md)

Read [AGENTS.md](AGENTS.md) and the relevant product usage documentation before changing source. Developers may use their own development harness; this repository contains the shared implementation, tests and usage contract.

For a change, explain the concrete problem, the resulting behavior, and what was verified. Keep refactors and unrelated cleanup separate. Update affected product documentation here; development plans, research, decisions and acceptance summaries belong in your chosen harness. Distinguish proposals from behavior backed by a real integration.

Set up the editable project and pinned developer tools with [uv](https://docs.astral.sh/uv/getting-started/installation/), Python 3.11 and Node.js for JavaScript fixtures:

```sh
uv sync --locked --python 3.11
uv run --locked --python 3.11 python tools/check.py all
uv run --locked --python 3.11 python tools/check_install.py
```

`check.py all` runs the quality gates and local regression suite. `check_install.py` builds wheel and sdist, rebuilds a wheel from sdist, and runs clean installs and regressions outside the checkout. The fixtures use fake hc, disposable repositories, local processes and loopback HTTP; they do not invoke real agents or models. To run only the regression suite:

```sh
uv run --locked --python 3.11 python -m unittest discover -s tests -v
```

To check a real installed hc without a model, follow the isolated [smoke instructions](docs/INSTALL.md#first-success-without-a-model). It creates a dedicated session; do not run experiments against a user's active work.

For ordinary bugs, use [GitHub Issues](https://github.com/M1racleShih/herdr-agent-bridge/issues) with a minimal reproducible example, versions, expected and observed behavior, and sanitized evidence. Keep local paths, transcripts and credentials out of shared reports. Report vulnerabilities privately through the channel in [SECURITY.md](SECURITY.md).

## Contribute a coding-agent adapter

Start with the existing [capability declarations](src/herdr_agent_bridge/agent_capabilities.py), [adapter](src/herdr_agent_bridge/agent_adapters.py), [native command handling](src/herdr_agent_bridge/native_commands.py), and the [development contract](docs/DEVELOPMENT.md). These are coordinated implementation points, not a stable plug-in API: a new agent also needs protocol selection, native evidence and lifecycle integration where applicable. Identify the agent version and supported runtime, startup and resume behavior, model selection, permission boundaries, and native commands before implementing.

Use synthetic, sanitized native-history fixtures and fake hc. Extend [adapter tests](tests/test_agent_adapters.py), evidence tests and [native-command contract tests](tests/test_native_commands.py) as appropriate. Cover exact submitted input, session and turn identity, final answer and completion ordering; reject missing, ambiguous, malformed or unsupported evidence. Test startup, follow-up, interruption and recovery only where supported, as well as permission prompts, ordinary-input conflicts, duplicate operation IDs and uncertain send or notification delivery. Unknown delivery must be reconciled, not replayed. Preserve existing journal formats, original paths and identities, and do not treat terminal `done` as verified success.

In a contribution, distinguish fixture results from a real integration. For a supported real-agent claim, separately record the OS, hc, agent and model versions, authorized setup, actual correlated native session/turn evidence, independent verification, failure or uncertainty cases, and cleanup of dedicated resources. A local runner does not prove Bot dispatch or delivery. Publish sanitized summaries only; keep credentials, raw transcripts, private project files, machine-specific configuration and runtime artifacts out of Git. Update the support documentation to match the evidence, and clearly mark untested features rather than implying support.

## Real interactive Codex acceptance

The local bridge and real-agent acceptance runner require **Python 3.11+**, `hc`,
`codex`, and existing working Codex authentication/provider configuration. The
read-only tests have passed on Linux with hc 0.1.22 and Codex CLI 0.171.0. The real check
uses model access; the unit tests above do not.

```sh
herdr-bridge tool accept_codex
```

This starts interactive Codex with `--no-daemon`, `read-only` and `on-request`
approvals in a private hc session. It independently checks source facts and
citations, follows up in the same native thread, handles a clarification, then
checks timeout, uncertain delivery and hc control cases. A real send reply is
locally discarded once; this is not a real network outage. The clarification
answer is supplied by the test script, not a live human. Control-case programs
are fixtures, not extra Codex runs. No permission or trust dialog is approved.

Run it in an environment where Codex can start its own sandbox. A nested Codex
sandbox may hide the shared socket directory and cause `blocked`; that is a
failed run requiring inspection, not permission to relax the inner sandbox.

The random session name, recovery directory and task intent are written before
side effects. Each run writes private evidence to `.artifacts/acceptance/codex/<run>/`:
`acceptance.json` (checks/results), `calls.json` (attempts), and
`bridge/journal.json` (task, terminal and native turn mapping). A private copy of this isolated run’s JSONL is saved before cleanup as
`codex-session-*.jsonl`. The independent
Codex home and XDG directories are temporary. Only provider configuration,
authentication and pre-existing trust for the exact project are reused; hooks,
plugins, MCP servers and unrelated project trust are not imported. This is
state isolation, not an isolation boundary against same-user processes.
Successful cleanup removes the temporary home, including copied authentication.
If cleanup fails, retain the journal and reconcile its exact terminal before
retrying cleanup; the recovery directory can still contain authentication.

The minimal bridge can also be driven manually:

```sh
herdr-bridge agent --run .artifacts/manual-run start --project "$PWD"
herdr-bridge agent --run .artifacts/manual-run observe --wait 20
herdr-bridge agent --run .artifacts/manual-run submit analysis-1 --prompt-file /tmp/task.txt
herdr-bridge agent --run .artifacts/manual-run observe --task-id analysis-1 --wait 20
herdr-bridge agent --run .artifacts/manual-run submit followup-1 --parent analysis-1 --prompt-file /tmp/followup.txt
herdr-bridge agent --run .artifacts/manual-run close
```

Wait for readiness before submitting, and inspect the first task until correlated
completed evidence is available before following up. Reuse of a task ID and a
new prompt while delivery is unresolved are refused. `needs_human` can mean an
Agent question or a native permission dialog; the tool never answers a dialog.
Terminal status and returned evidence do not set `verified_success`: a caller
must perform independent checks through `Bridge.verify`, as the acceptance
runner does. This is a local, single-writer adapter; it has no Grok transport,
notification queue or automatic recovery/replay.

Optionally render a compact Chinese-language HTML report from a **passed** acceptance record:

```sh
herdr-bridge tool render_codex_report .artifacts/acceptance/codex/<run>/acceptance.json \
  --output .artifacts/acceptance/codex/report.html --regression-count 34
```

The HTML contains a sanitized summary and the private record's SHA-256. Keep
raw transcripts, provider configuration and machine paths out of shared records.
The renderer also supports failed records and must label them as not passed.

## Grok management boundary

Use `herdr-bridge manager probe` for isolated local readiness and the
existing unittest command for regression. Both avoid models and user sessions.
The manager wraps the local agent bridge with a fixed Bot/conversation binding,
compact evidence output, deterministic acceptance contracts, and a durable
official-routine webhook outbox. See [the operational contract](docs/GROK.md)
for commands, private credential format, observer lifecycle and recovery.

The first `idle` report can precede input readiness in hc 0.1.18. The manager
waits out that version's 10-second startup guard using the terminal creation
time and checks live state again; require `input_ready=true` before submission.
No startup send is attempted while that condition is false.

Tests use fake hc, disposable journals and loopback HTTP fixtures. A locally
started real Codex check also does not establish Grok-originated dispatch or
Grok receipt. Grok integration acceptance needs real Grok conversation/local-tool evidence,
correlated source verification, an actual question/answer continuation, webhook
and receipt association, and cleanup of the dedicated core, copied temporary
authentication and observer process. Keep those raw records in `.artifacts/`.

## Pi integration

Use `herdr-bridge manager probe --agent pi` and the same local unittest
command. Pi parser/bridge/SDK-launcher tests use disposable native-history
fixtures, fake hc, and synthetic SDK packages; they do not access real auth or
models. The SDK fixture tests invoke Node.js and skip if it is unavailable.
Networking fixtures also substitute Pi's SDK-relative public undici dependency;
they check proxy/timeout behavior and dispatcher cleanup without network calls.

`herdr-bridge tool accept_pi` invokes the configured real model and keeps its
own hc session, settings, cache and native history. API keys are copied only for
the selected provider; OAuth uses Pi's original native auth file and normal
lock/refresh semantics, so it requires authorization for that persistent refresh.
Never refresh OAuth in a disposable copy and delete the replacement token.
Trust is declined, executable resources disabled, and only read/grep/find/ls
are exposed. This tool selection is not an OS sandbox.

The real local check covers four deterministic turns, source/citation checks,
discarded-send-reply reconciliation without replay, and cleanup. Its language
answer is preset and it does not invoke Grok. Before cleanup it snapshots this
run's native history on success or failure, without copying auth into the
evidence directory. See [Pi operation and evidence boundaries](docs/PI.md).

## Reproducible quality gates

Use Python 3.11 and the lockfile for pinned developer tools:

```sh
uv sync --locked --python 3.11
uv run --locked --python 3.11 python tools/check.py all
```

`lint`, `format`, `types`, `naming` and `test` run separately in CI through the same
`tools/check.py` entry. Ruff checks E4/E7/E9/F and import order across Python
code; formatting is a separate gate. Regression runs on Python 3.11 and 3.15
with Node 24, including the existing JS fixtures through unittest.

Mypy currently checks `bridge_common`, `bridge_protocol`, and `development_task`,
with normal import following and checks inside untyped functions. Persistence
and hc transport have fully annotated signatures and reject unparameterized
generics. Policy selections and verification tickets constrain model and state
boundaries. Dynamic journal dictionaries, most state methods, adapters and CLI
modules remain incremental typing work; this is not full strict coverage.
No missing-import suppression or skipped-import replacement is used.

The `naming` gate checks filenames and text for development-stage names. Narrow
historical-input exceptions are recorded as exact lines with reasons in
`tools/naming_exceptions.json`; new public aliases and output paths are not exceptions.
Protocol state fields named `phase` and upstream version numbers are unchanged.
