# Pi integration

English | [中文](PI.zh-CN.md)

Pi can use the `Grok Bot → manager CLI → hc` path. For daily native
development see the [fixed project entry](PROJECT.md); for development
contracts and takeover see the [development interface](DEVELOPMENT.md). This
page describes lower-level `isolated` sessions, read-only tools and
authentication. The lower-level manager defaults to Codex and treats old
manager journals without an `agent` field as Codex. The fixed project entry
has its own defaults.

## Local preflight and start

Python 3.11+, Node.js, hc, and Pi installed with a configured model are required.
The recorded baseline is Linux, Pi 1.5.0 and hc 0.1.18; revalidate other
versions with the actual integration.

```sh
herdr-bridge manager probe --agent pi
uv run --locked --python 3.11 python -m unittest discover -s tests -v
```

Preflight queries hc and Pi versions with temporary HOME/XDG/Pi configuration.
It needs no Codex installation, reads no credentials and starts no agent; it
does not establish model or Grok availability.

Use the registered local entry in the repository root to start a new dedicated run:

```sh
herdr-bridge manager --run .artifacts/runs/<unique-pi-run> \
  --bot-id <bound-bot-id> --conversation-id <bound-conversation-id> \
  start --agent pi --project "$PWD" --webhook-config <private-config-file>
```

Notification configuration can initially be omitted. New runs use
`.artifacts/runs/` regardless of agent or caller.

Pi reads saved `defaultProvider`, `defaultModel` and reasoning level from
`PI_CODING_AGENT_DIR`, or `~/.pi/agent` by default. Override with
`--model <provider>/<model-id>`; saved model IDs can contain `/`. A missing
model is an explicit error, not a fallback to the Codex default.

After receiving `agent=pi`, use the same run, Bot and conversation and
`--expect-run-id` for `observe`, `submit`, `verify`, `ack` and
`close`. The run fixes its agent; subsequent calls do not select it again and
a restart command cannot switch it. Wait for `input_ready=true` before input.
See the [manager contract](GROK.md) for full commands and notification recovery.

```sh
herdr-bridge manager --run .artifacts/runs/<unique-pi-run> \
  --bot-id <bound-bot-id> --conversation-id <bound-conversation-id> \
  --expect-run-id <returned-run-id> \
  submit analysis-pi --contract analysis --monitor
```

Custom work uses `--prompt-file` and requires its own independent acceptance;
do not apply unrelated built-in questions. The lower-level local entry also
supports `herdr-bridge agent --run <private-run> start --agent pi --project <project>`.

## Configuration, authentication and execution boundary

- hc uses a random dedicated session and temporary XDG paths; Pi uses separate
  configuration, model cache and session directory. Only model choice and that
  provider's model configuration are carried in. User extensions, packages,
  MCP, trust, global instructions, history and UI settings are not copied.
  Project and ancestor context files still follow Pi behavior.
- `httpProxy`, `httpIdleTimeoutMs`, `websocketConnectTimeoutMs` and
  `transport` network settings are retained. Before creating ModelRuntime,
  the SDK initializes dispatcher/fetch using undici from the Pi installation.
  Existing environment proxies take precedence, `NO_PROXY` is respected and
  proxy addresses are not output. The process dispatcher is released on close.
  Proxy configuration can contain credentials and stays in private temporary
  configuration.
- Only `read,grep,find,ls` are exposed. Extension, MCP, skill, prompt-template
  and theme discovery is disabled; project configuration requiring trust is
  refused. Automatic compaction and agent retry are disabled. API-key mode
  uses the official interactive CLI; OAuth uses the official SDK
  `InteractiveMode`.
- API-key authentication copies only the selected provider into a mode-600
  temporary file and removes it on close. OAuth **does not copy tokens**: the
  SDK `ModelRuntime` uses the original auth file's `authPath`, and Pi's
  native file lock controls refresh and persistence. The original auth file is
  retained and updated as in ordinary Pi use; cleanup removes only the
  temporary pointer and this run's resources.
- OAuth shares the entire native auth store, not a provider-isolated vault.
  Pi may read other providers' authentication for availability checks; the
  actual task uses the chosen model. Before starting, the selected model and
  API-key configurations in the shared store are checked and `!command`
  values are rejected. This is a snapshot, not a continuing constraint on
  other processes changing the auth file.
- Inherited hc/Pi session markers and Node preload configuration are filtered.
  Normal environment needed for the model, such as PATH, proxy, certificates
  and provider variables, is retained.
- Read-only tool selection, dedicated state and refused project trust do not
  constitute an OS sandbox. Pi retains the launching account's permissions,
  and read tools have no hard project-directory boundary. This is not Codex's
  `read-only/on-request` sandbox; the tool does not approve permission or
  trust prompts.

## Result evidence

`pi_evidence.py` reads one Pi v3 JSONL session from this run's separate
directory. It exactly matches the full submitted user text and retains the
native session ID and user-message entry ID. Pi has no Codex-style turn ID:
`agent_turn_id` uses the actual user entry ID, and private evidence marks
`turn_id_source=pi_user_message_id`.

Normally only the last valid assistant text after that user message and before
the next, with `stopReason=stop` and no pending tool call, counts as native
completion. For a **native Pi development result** only, a narrowly recognized
Herdr two-pack can continue across its Coder and Cleaner receipt notifications:
the linear history must first show one successful initialization bound to the
original input and the session's working directory, followed by a matching
pack/worker binding before the first notification. Matching, non-error tool calls and structured results must also
corroborate the accepted receipts. The recognized snapshots include the direct
structured resume and the observed projected form; other formats remain
uncorrelated. Unrelated terminal inventory entries do not change the workers.
The final must be a result for the original task. An independent input,
uncertain or mismatched receipt, question, or an existing bounded final does
not qualify. The original user entry remains the turn ID; the actual final
assistant entry remains the final message ID. Isolated Pi and ordinary direct
turns keep their existing behavior.

This checks transcript consistency, **not cryptographic sender authenticity**:
a participant with control of native history or tool output could forge the
observations. The bridge neither runs commands nor opens files named in the
notifications to establish this correlation. A supported transcript shape is
not proof the work succeeded; changed or unknown formats remain uncorrelated.
The hc turn must also advance and reach idle/done, and native Pi must settle on
the same final message. Questions, task results and independent verified success
remain distinct; CLI success or terminal done is not task success.

Multiple sessions, duplicate matches, branches, forks, context edits,
compaction, unknown formats and mid-file corruption do not fall back to screen
regex guesses. An incomplete final record cannot complete the current task;
observe the original instead of redispatching. Only short linear sessions are
supported here; revalidate the format and SDK behavior after Pi upgrades.

## Repeatable real local acceptance

```sh
herdr-bridge tool accept_pi
```

This invokes the configured model, creates dedicated resources, and performs
analysis, same-session follow-up, a deterministic language question and
continuation with a scripted English answer. It uses the management layer for
source/citation verification. Once, after a real send returns, the local reply
is discarded and the original task reconciled without resending. This is not
an actual network outage or an actual human answer.

Results, native-session snapshots and call records are saved under
`.artifacts/acceptance/pi/<acceptance-id>/`; the report's `run` points to
Manager and Bridge state under `.artifacts/runs/<run-id>/`. On failure the
runner also tries to save native evidence before cleaning dedicated processes
and temporary auth; a snapshot may include an incomplete final record. Raw
sessions may contain project contents and remain private in `.artifacts/`.
OAuth refresh needs the original auth directory writable. If the outer
environment refuses, retain the failure, do not bypass authorization, log into
another account or blindly replay work.

Local acceptance and actual Grok dispatch/notification landing are separate.
Neither a local coordinator run nor fixtures establish a Grok → Pi integration.

## Sources

- [Pi CLI and tool options](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)
- [Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)
- [Authentication SDK example](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/examples/sdk/09-api-keys-and-oauth.ts)
- [Session format](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)
- [Pi security and project trust](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)
