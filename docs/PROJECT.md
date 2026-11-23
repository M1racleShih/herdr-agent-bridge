# Fixed project entry

English | [中文](PROJECT.zh-CN.md)

`herdr-bridge` provides a persistent entry for one project, reusing its Manager,
run, native agent session, development-task record and notification outbox. The
project configuration holds identity, execution policy, named checks and run
pointers; it does not create a second task state machine.

This guide describes the local interface and recovery rules. Fixture tests do
not establish a real Bot, agent or App integration. See the
[Bot and routine templates](PROJECT_BOT_TEMPLATE.md) for caller instructions.

## Initial binding

Install Python 3.11+, hc, and the chosen agent's CLI and native authentication.
The daily entry uses `native` by default: it creates the task terminal in the
normal core and follows the agent's global and project resources, permissions
and trust behavior. Pi keeps its session in a run-specific directory; Codex
keeps its native record in its own session directory and the run stores the
exact thread binding. Review writable paths, check commands and synthetic-data
boundaries. `init --runtime isolated` is for dedicated acceptance work.

Existing configurations retain their runtime. `runtime native` changes only
new requests. After `app-space SPACE_ID`, new native requests attach to the
existing App and specified space. The same choice can be made at initialization
with `init --app-space SPACE_ID`. Without it, bind manually with
`herdr-bridge app --run RUN start` and then `bind --space SPACE_ID`.
Do not create a second App or close the user's space or shared core.

Save a local named-check file, for example `checks.json`:

```json
[
  {"name": "unit", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"], "timeout": 120}
]
```

Check names must be unique, `argv` is the complete argument array, and each
timeout is 1–120 seconds. Checks execute project code: review their actual scope
before enabling them. Do not silently add dependency installation, long-running
previews or cloud writes as short checks.

The fixed command prefix is:

```sh
herdr-bridge --config /private/path/project.json
```

Run the complete command once to initialize it:

```sh
herdr-bridge --config /private/path/project.json init \
  --project-id sample-app --project /absolute/path/to/app \
  --bot-id PROJECT_BOT_ID --conversation-id PROJECT_CONVERSATION_ID \
  --webhook-config /private/path/webhook.json --checks-file /private/path/checks.json
```

Paths and identities are placeholders. Keep real files in private local
locations or ignored `.artifacts/`. The project root is normalized; the
configuration is written with mode `0600` and `init` will not overwrite an
existing configuration. The optional webhook file follows the
[Grok management interface](GROK.md): it contains the HTTPS endpoint, key
and matching Bot/conversation identity. The project configuration stores only
its reference.

This credential-free structure illustrates the configuration normally created
by `init`; do not handwrite run records:

```json
{
  "format": 1,
  "project_id": "sample-app",
  "project_root": "/absolute/path/to/app",
  "owner": {"bot_id": "PROJECT_BOT_ID", "conversation_id": "PROJECT_CONVERSATION_ID"},
  "webhook_config": "/private/path/webhook.json",
  "defaults": {"model": "openai/gpt-6.1-sol", "thinking": "high"},
  "checks": [
    {"name": "unit", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"], "timeout": 120}
  ],
  "starts": {},
  "active_request": null
}
```

## Bound adapter for a Bot

An operator can put a thin adapter in a private local file to fix the project
configuration and forward project commands:

```python
from pathlib import Path
from herdr_agent_bridge.project_entry import run_bound

raise SystemExit(run_bound(Path('/private/path/project.json'),
                          audit_dir=Path('/private/path/entry-calls')))
```

The Bot can then use `python3 /private/path/project-command.py` as its prefix.
The caller supplies task IDs, rounds and prompts; the adapter refuses a
`--config` override and the same `project_bridge` manages state. Create a
separate configuration and adapter for another project, without copying task
logic. Updating the adapter does not require a new webhook.

The call audit records operation, time, process origin and return status, not
arguments, prompts, output or configuration. An audit failure before execution
returns not attempted; after execution it returns unknown delivery, which is not
fixed by replaying input. The audit is neither caller authentication nor a
tamper-proof credential.

`app-space SPACE_ID` affects new requests only. Each request retains its own
choice; repeated `start` and `status` read the saved App association rather
than launching or binding again. `app.evidence=saved_app_state` does not prove
the window is still visible. If App attachment returns `pending/error`, reconcile
the original run and session rather than starting another agent.

## Local project discovery and confirmed registration

A trusted local operator can use `ProjectRegistry` with a private registry
file and `allowed_roots`, supplied as absolute paths without symlinks. Callers
may share a registry while supplying their own currently authorized roots. The
registry stores neither caller permissions nor task state.

```python
from herdr_agent_bridge.project_registry import ProjectRegistry

registry = ProjectRegistry("/private/projects.json", allowed_roots=["/path/to/work"])
choice = registry.resolve("report-app")
```

`resolve` first looks for an authorized registered name (case insensitive)
or project ID. `registered` can be selected; `ambiguous` requires a choice;
`not_found` means none was found within the scan; `incomplete` indicates a
scan limit or unavailable directory and cannot establish uniqueness. A unique
unregistered candidate returns `needs_confirmation` and its full root for the
person to review. Resolution neither writes the registry nor starts a task.

`discover()` returns candidates, their `.git` basis, any registered ID and
scan coverage. By default it searches up to four levels, 100 candidates and
10,000 entries. It does not descend below a project root, follows no symlinks,
skips hidden directories, common dependencies, virtual environments and build
directories, and checks only whether `.git` is a directory or regular file.

Only after the person confirms the **specific directory** may the operator call
`registry.register_confirmed("/path/to/work/report-app")`. Registration
rechecks path and permission; repeated registration of the same root returns
the existing ID. An exact non-Git path may also be explicitly registered.
Registration identifies a project; it grants no code execution or task changes.

## Personal Agent entry

A local operator prepares a private `0600` JSON binding for a caller and
conversation:

```json
{
  "format": 1,
  "registry": "/private/projects.json",
  "allowed_roots": ["/path/to/work"],
  "owner": {"bot_id": "BOT_ID", "conversation_id": "CONVERSATION_ID"},
  "projects": {"REGISTERED_PROJECT_ID": "/private/existing-project.json"}
}
```

The registry can be shared, but each entry sets its own authorized roots,
conversation and execution bindings. `projects` may initially be empty.
After the person confirms the exact directory, the operator can register it
(`--name` is also supported):

```sh
herdr-bridge personal --binding /private/caller.json register /path/to/work/report-app
```

A configured Hermes Desktop Bot Chat can instead use the `/hab-register`
confirmation card below. Other platforms need a trusted operator or their own
authenticated confirmation receiver. Do not expose
`personal_entry.register_confirmed`, the operator command or an entry with a
changeable binding as an agent tool.

The operator must review the execution configuration and named checks, map the
**returned registration ID** to the existing configuration, and
[enable the shared project slot](#shared-project-start-slot) before starting
new work. The configured root and Bot/conversation must match the registration
and current caller. Existing requests, runs, owners and webhooks need no
rewrite. Discovery or registration alone grants no execution rights.
`project_not_bound` means an execution binding is missing;
`shared_start_required` means the operator has not enabled the shared slot.

A single thin agent wrapper fixes the binding for all projects:

```python
from herdr_agent_bridge.personal_entry import run_bound

raise SystemExit(run_bound("/private/caller.json"))
```

Use `resolve NAME_OR_ID` or `discover` to select a project. Task commands
`start`, `status`, `result`, `submit`, `native-command`, `verify` and
`close` require explicit `--project-id` and `--request-id`; `start` accepts
`--agent pi|codex`. `start --development` reads development-contract JSON
from standard input and `submit --round-id` reads prompt text from standard
input. Each is UTF-8 up to 64 KiB; arbitrary input-file paths are not accepted.
Use a new `--round-id` and `--parent-round-id` for continuation, and
`status/result --round-id` for a specified round. `--observe` is bounded.
Use `verify` for development and specify `--round-id` when handling a
completion event: if another round has been added, checks are refused rather
than attributed to the old round. Preset read-only acceptance uses
`verify --round-id` with `--contract`. Tell the person to await a callback
only when the original entry has notifications and returns `await_callback`.

Every task call rereads the boundary configuration and checks registration,
root authorization and execution binding. Revocation blocks new CLI access,
but does not interrupt operations already running. Authorized operators retain
the original records and reconcile and close them through the original entry.
This entry does not replace notification, takeover or human-acceptance
interfaces; closing or verifying a task does not mean the person accepted it.

## Hermes Desktop registration card

For a bound Desktop Bot Chat under the same local user, optionally install the
`hab-project-registration` plugin. Copy `plugin.yaml` and `__init__.py`
from the installed `herdr_agent_bridge.resources.hermes_project_registration`
package to the target profile's `plugins/hab-project-registration/`, not the
shared default profile. Locate the installed directory with:

```sh
python -c 'from importlib.resources import files; print(files("herdr_agent_bridge.resources.hermes_project_registration"))'
```

The operator writes a private `0600` `settings.json` in that plugin directory:

```json
{
  "format": 1,
  "profile": "NAMED_PROFILE",
  "session_id": "ORIGINAL_BOT_CHAT_SESSION_ID",
  "chat_id": "LOCAL_CHAT_ID_FROM_HERMES",
  "user_id": "AUTHORIZED_LOCAL_USER_ID_FROM_HERMES",
  "binding": "/private/caller.json",
  "bridge_python": "/absolute/bridge-venv/bin/python"
}
```

Obtain `session_id`, `chat_id` and `user_id` from the bound Hermes Bot
Chat's session metadata, not a display name or task text. The binding owner must
match the profile and `session_id`. `bridge_python` must be the bridge's
installed environment entry, retaining the virtual-environment path. Enable only
in the intended profile with
`hermes -p NAMED_PROFILE plugins enable hab-project-registration`; no model
tool needs registration.

After the Bot shows candidates, the **person in the original conversation**
enters `/hab-register NAME_OR_ABSOLUTE_PATH` and chooses **Register this
directory** on the Hermes card. The card shows the exact path; ordinary text
such as “I agree” or a background notification cannot replace it. Resolve name
collisions to an exact path first. A still-authorized registered directory
returns its existing ID. Cancellation or expiry does not register; authorization
and directory identity are reread before commit, and replacement requires a
new confirmation. Registration neither sets an execution binding nor starts
work. If the result is uncertain, inspect the original directory; do not replay
from the model.

The trust boundary is the Hermes-authenticated local viewer and operator-held
configuration, not an OS sandbox. Do not give an untrusted model the same
account's raw Hermes control socket, plugin settings, registry, operator
registration command or unrestricted local execution that bypasses the fixed
entry. Other Hermes messaging platforms and Grok conversation confirmation are
not supported by this mechanism. Keep card expiry bounded.

## Hermes Desktop Bot Relay

When bridge and Hermes run under the same local user on one machine, choose
Relay **when creating a new project execution binding** with
`init --notification-config /private/hermes-relay.json`. Existing Grok HTTPS
and `--webhook-config` remain available. Relay starts no gateway, opens no HTTP
port and needs no copied Hermes credentials. The operator maintains a private
file, preferably mode `0600`:

```json
{
  "transport": "hermes_relay",
  "bot_id": "NAMED_PROFILE",
  "conversation_id": "ORIGINAL_BOT_CHAT_SESSION_ID",
  "profile_home": "/absolute/hermes-home/profiles/NAMED_PROFILE",
  "hermes_source": "/absolute/installed/hermes-agent",
  "hermes_python": "/absolute/hermes-runtime/bin/python3"
}
```

Use a running named profile and its **bound local Desktop Bot Chat**, not a
title, most recent conversation or unbound HTTP-created session. Both IDs must
match the owner passed to project `init`. For the personal entry,
`init --project-id` must use the returned registered ID. New runs pin the
profile, conversation, project and request; old runs are not migrated. Match
the Hermes runtime to its installation and revalidate the Relay interface after
upgrades.

The observer started by native `submit` writes result, question and failure
events to a durable outbox and delivers each with the same event identity.
Notifications carry event, project, request, run, round and kind, not prompt,
source or result text. They add no Bot permissions. The initiating user turn
may authorize reads and named verification for that task, but the Bot must
still match the request and round. A background event cannot confirm
registration, dispatch work or answer permission prompts.

`notify EVENT --request-id REQUEST` queries the original receipt for accepted
or uncertain Relay delivery; it does not resend. `relay.status=queued/claimed`
means Hermes has taken the event, not that it replied; `settled` means that
turn completed, not that the task was verified or accepted. Only
`rejected/not_sent` may be explicitly retried with `--retry` after the
operator repairs the original route. Reconcile an `unknown` receipt first;
never automatically redeliver or reroute it. `ack` remains for the Grok path
and a manual receipt does not replace actual Hermes state.

## Shared project start slot

One normalized project root uses one shared start slot. Aliases, Bots and
conversations may differ; sharing the slot grants no cross-binding read or
continuation rights. Fixed entries retain their original behavior until enabled.

First update every dispatching entry to a version supporting shared starts.
Then the local operator enables it via Python, supplying **all existing**
configurations for the project, especially those with active or uncertain tasks:

```python
from herdr_agent_bridge.project_bridge import Project

Project.enable_shared_starts("/private/old-project.json", "/private/another-existing-entry.json")
```

Initialization saves only the slot and current request pointer in
`.artifacts/project-admission/slot.json`, not a caller list. If multiple
unclosed or uncertain historical tasks are found, reconcile and close them
first. Existing configuration, owner, run, journal and outbox remain in place.
Later starts check the same slot rather than scanning other configurations;
repeated initialization does not replace it. Do not delete the slot or bypass
the gate with an old version. Low-level `agent` and `manager` debugging
interfaces do not participate in the slot and are not substitutes for normal
dispatch.

When two entries start simultaneously, only one records intent and creates a
run under the project lock. Others receive `project_start_conflict` for an
active run or `project_admission_unresolved` when occupancy cannot be
reconciled, both with `delivery=not_attempted` and without exposing another
conversation's task identity. Reconcile existing work through its original
entry; start new work only after its run and dedicated resources are confirmed
closed. Normal close writes `run-closed.json` in the run, which the slot checks
before the full journal. Older runs without that receipt use their original
close record; these run records are not subject to the configuration's 4 MiB
read limit.

### Interrupted start without a run

Inspect the original entry and `REQUEST_ID` first; do not resend if there is
a run or evidence of creation. The local operator may use the **original
configuration**:

```sh
herdr-bridge --config /private/original-project.json abandon-start REQUEST_ID
```

Abandonment is allowed only before a run is bound and when non-start can be
proven. With a shared slot, an absent or genuinely empty target run directory
is one such proof: Manager writes and syncs its creation record before touching
`hc`. Another recoverable case is a new request with only an initial Manager
record and no Bridge, while the original development slot of the same entry is
still active even though its run is closed. Abandon the **new request** first,
then explicitly terminate the **old task**:

```sh
herdr-bridge --config /private/original-project.json decide terminate \
  --request-id ORIGINAL_REQUEST --actor OPERATOR --note 'original task ended'
```

Neither action starts an agent or deletes records. Use a new request ID for
later dispatch. `abandon-start` is not exposed through the Bot's fixed entry.
A Bridge, bound run or historical unknown without this evidence requires
reconciliation through the original entry.

## Start, continue and close

Existing projects default to Pi, model `openai/gpt-6.1-sol` and `high`
reasoning. `config` reports `agent` and `defaults`, hiding project path,
Bot/conversation binding and notification configuration path.

Use `start REQUEST_ID` for read-only investigation. For development, use
`start REQUEST_ID --development-contract /private/path/task.json`, for example:

```json
{
  "task_id": "repair-report",
  "goal": "Fix the confirmed calculation error in the report",
  "allowed_paths": ["src/report.py", "tests/test_report.py"],
  "acceptance": ["Existing synthetic data covers the fix and existing tests still pass"],
  "checks": ["unit"]
}
```

The goal and acceptance strings are examples. For non-ASCII text, the default
serializer uses `ensure_ascii=False` while escaping JSON correctly. It applies to new rounds and
does not rewrite submitted input or terminal history. Contract `checks` may
only reference configured check names; unknown or duplicate checks and
out-of-scope or symlink paths are rejected. `start` creates a dedicated
execution session but does not submit a business prompt. If scope is unclear,
investigate read-only before writing a development contract.

All commands use the same configuration prefix:

| Command | Purpose |
|---|---|
| `status --observe --wait 10` | Bounded observation of execution and correlated results; timeout is not success. |
| `submit ROUND_ID --prompt-file /private/path/prompt.txt --monitor` | Submit a round to the original run and start bounded observation. |
| `submit NEXT_ROUND --parent PREVIOUS_ROUND --prompt-file /private/path/followup.txt --monitor` | Continue the same task with the original round association. |
| `native-command OPERATION_ID COMMAND --request-id REQUEST_ID [--arguments TEXT]` | Send and check an explicit native command; operation and business-round IDs are separate. |
| `result` | Read saved latest state and evidence without rerunning work. |
| `interrupt ROUND_ID --actor OPERATOR --note 'change direction'` | Stop the current round and retain the session; after `interrupted`, continue with a new round and the original `--parent`. |
| `recovery-status` | Check process changes and return code, native-session and terminal review fingerprints. |
| `recover --fingerprint VALUE --actor OPERATOR --review TEXT` | After review, start the bound native session in the recovered shell. |
| `verify` | Independently gather development scope diff, named checks and final code fingerprint. |
| `decide accept --actor OPERATOR --note 'reviewed evidence'` | Record technical acceptance after passing verification if code is unchanged. |
| `decide reject --actor OPERATOR --note 'needs correction'` | Retain the task and record that correction is needed. |
| `decide terminate --actor OPERATOR --note 'task stopped'` | Terminate development explicitly; may be recorded after close without reopening. |
| `close` | Clean this run's dedicated resources while retaining work and evidence; it does not replace acceptance or termination. |

Input and `run_ready` are enabled only after the native agent confirms actual
model, reasoning and session ID. `model_selection.requested` is the request;
`actual` is confirmed; `state: pending` waits and `state: failed` means
native selection failed. Subsequent rounds use evidence from the bound
session. `execution_policy` records requested, resolved and source values.

Pi requires the native assistant message's provider/model to match the startup
selection. A virtual model routed to a different actual provider/model returns
`native_dispatch_mismatch` and is not supported.

If `--monitor` returns `monitor_error`, inspect the observer; do not resend
`submit`. With native notifications configured, `submit` normally starts a
persistent hc observer independent of the Bot's Shell process.
`next_action=await_callback` means the Bot should end its wait while the
observer waits and calls back, not poll status. Recover an observer after a
`monitor_error` without redispatching delivered work. On callback validate
identity and latest state once, then retrieve, verify and report; manual status
and bounded recovery remain available.

Native observers use `hc events --terminal ADDRESS` and report
`monitor.observation=events`. Initial snapshots, changes, snapshots after
lost events and reconnects each trigger reconciliation. Sequence numbers
deduplicate each connection, reconnect starts with a fresh snapshot, and the
durable outbox deduplicates notifications. `monitor.subscription` summarizes
connections, reconnects, events and duplicates. Bounded follow-up checks cover
native evidence still being written after terminal work ends.
`monitor --seconds 600` can start observation independently, up to 3600
seconds; do not duplicate an observer whose termination is unconfirmed.
Submission receipt, terminal observation, correlated result, verification and
operator acceptance are separate facts; terminal `done` or passing checks do
not replace the person’s actual experience and final acceptance.

Run commands can use `--request-id REQUEST_ID` to read an older binding;
`status` and `result` support `--task-id ROUND_ID`. Read-only `verify`
requires `--task-id ROUND_ID --contract NAME` and applies only to preset
read-only acceptance questions, not arbitrary product requirements.

For App takeover use `takeover --actor OPERATOR`, `reconcile` and
`resume --fingerprint VALUE --review TEXT`. Automatic input pauses; inspect the
original session and current code before resuming. Never answer permission or
trust dialogs for a person. See the [development interface](DEVELOPMENT.md).

`interrupt` saves the operator, reason and original round before sending
one stop per round. Repeated calls return the original record. If receipt is
uncertain, use `status --observe` to reconcile native interruption and
terminal state. Once confirmed, the round shows `interrupted`, with its result
still unaccepted. Native Pi also checks `agent_settled` before accepting
further requirements in the same session.

New native terminals enable hc `keep`. After process loss, core restores a
shell at the same address; the bridge detects the change, sets
`recovery.state=needed` and pauses input. Review `recovery-status` file
summaries and fingerprint before `recover`. Recovery starts only the bound Pi
session file or Codex thread. `restored` requires model, reasoning and session
identity to match in the new process. The original round stays interrupted;
continue with a new round and `--parent`. Observe the original run on an
uncertain receipt; repeated `recover` returns the original recovery record.
Supported recovery shells are sh, bash, zsh and dash.

## Verification, takeover and cleanup

Development verification saves fingerprints of the original round, code and
native session, releases the state lock, then runs named checks. `status`
remains available and `development.verification_job` shows the job;
`input_ready=false` and new input or duplicate verification is rejected
rather than silently queued.

`takeover` pauses automatic input and cancels running verification. After
the check process group is stopped and reaped, verification returns
`passed=false`, `reason=verification_cancelled`; its prior result cannot
support acceptance. `reject`, `terminate`, or changes to native session or
code also invalidate verification.

`close` first requests verification cancellation. If a check still holds the
independent verification lease, it returns `closed=false` and a failed cleanup
item. Inspect the original run, wait for verification to finish, and retry
`close` on that run. If the verification parent dies, the child-held lease
still prevents a rerun or premature cleanup. After confirming release, explicit
`reconcile` may mark the stale job `verification_interrupted`; it does not
rerun checks or submit work.

Named checks must be bounded. If the main check exits leaving process-group
members, the group is terminated and `check_left_descendants` prevents a pass.
Manage preview services separately. This is not a malicious-process sandbox and
cannot guarantee capture of a process that deliberately leaves the group.

Pi waiting/failure reasons appear in `model_selection.reason` and round
`diagnostic`, for example `native_session_missing`, `native_trailing_write`,
`native_version_unsupported` and `native_history_ambiguous`. Codes contain
no raw records or private paths. Continue observing missing or partial writes;
inspect explicitly unsupported records. Do not redispatch.

Failure JSON retains `error`, `delivery` and recovery guidance and adds
`code` and `operation`; for example, invalid model policy returns
`execution_policy_invalid`. These fields identify the failure stage and do
not change `delivery=unknown` into not delivered.

## Defaults and per-request overrides

`start REQUEST_ID --agent pi|codex --model MODEL --thinking LEVEL` overrides
only that start. Omit `--agent` for the project default. Pi uses
`PROVIDER/MODEL`, Codex its native model ID, for example `gpt-6.2-sol`.
`--reasoning` is equivalent to `--thinking`. Codex development needs the
`native` runtime.

For a new configuration, `init --agent codex` selects Codex by default.
`defaults --agent codex` changes the project default, and
`defaults --model MODEL --thinking LEVEL` can set model and reasoning.
Unspecified fields for the same agent inherit current project values. When
changing the default agent, or explicitly selecting another agent for a start,
unspecified fields use that agent's built-in defaults: Pi
`openai/gpt-6.1-sol/high`, Codex `gpt-6.1-terra/max`.
`execution_policy.sources` labels each field `task_override`,
`project_default` or `agent_default`.

Each agent validates model format and reasoning. A pre-start error returns
`execution_policy_invalid` and `delivery: not_attempted`, leaving the
project slot available. Actual values require native evidence. Defaults apply
to new requests only and changing them does not change global agent settings.

## Native commands

For a user-requested operation on the current session, use a stable operation ID:

```sh
herdr-bridge --config /private/project.json native-command mode-fast /fast --request-id REQUEST_ID
herdr-bridge --config /private/project.json native-command goal-one /goal --request-id REQUEST_ID --arguments 'Reduce report generation time'
```

The Personal Agent wrapper also supports `native-command` with explicit
`--project-id` and `--request-id`. A Pi session can be named with
`native-command OPERATION_ID /name --arguments TITLE`. Business input still
uses `submit`; mentioning `/fast` in its body is ordinary text. After
enabling Codex Fast, inspect `native_operation.state` and confirm it took
effect before submitting work that requires Fast. Reconcile selections,
working or unknown outcomes with the original request and operation ID.
`/goal` may start its own model turn; wait for its definite result before
submitting the next business round.

## Restart and uncertain delivery

Each start needs a stable `REQUEST_ID`. Retrying the same ID with the same
start parameters recovers the original run; changing agent, model or reasoning
returns `project_start_mismatch`. Omitting `--agent` on retry retains the
original agent. Repeat any original model and reasoning overrides; originally
omitted fields keep their saved resolved values. Old fixed-entry records
without an agent field use the Pi default in effect then. A second active run
cannot start before the previous run closes; use a new request ID afterward.
An old closed request ID does not restart.

Start intent is persisted before any Manager/hc side effect. After interruption,
timeout or lost receipt, recover the original binding via `status` or
`status --observe`. If intent exists but `manager.json` does not, the
entry returns `creation unresolved` and retains the pointer. Do not delete the
binding, change ID or create another run to bypass uncertainty. The operator
must inspect the dedicated directory and actual session and reconcile any side
effects.

Unknown input delivery retains the original task gate. Do not blindly resend.
`delivery: unknown` is conservative recovery guidance, not proof of
non-delivery. The CLI does not expose raw exceptions, commands or tracebacks.

## Notifications and privacy

A routine saves each payload to a unique private file, then calls
`notification --payload-file /private/path/unique-event.json`. The entry treats
the notification as an untrusted pointer and handles only runs already bound to
this project: new runs use `.artifacts/runs/RUN_NAME`, and historical paths
are read in place as described in the
[installation guide](INSTALL.md#runtime-and-acceptance-directories). Both
validate owner, run ID, event ID and saved event identity. Callback paths cannot
read arbitrary files, create a task or replay a prompt.

Subsequent `status`, `verify`, `result` and `ack` must explicitly use the
returned `--request-id REQUEST_ID`; never replace a late event's original
request with the current one. Active runs refresh status before results; closed
runs read saved state without observing or submitting. Record receipt using
`notification ... --receipt RECEIPT` or `ack EVENT_ID --receipt RECEIPT`,
which follows the same validation. `notify EVENT_ID` delivers an existing
outbox event; `--retry` applies only to explicit rejection, not uncertain
delivery. HTTP acceptance, routine receipt and task success are separate.
A composite receipt does not prove a platform-native message ID or independent
UI landing.

CLI checks, contract and callback JSON files are limited to 64 KiB. Duplicate
keys, non-finite numbers, invalid encoding and excessive nesting are rejected.
Project configuration is limited to 4 MiB and must be a non-symlink regular
`0600` file. Symlinked run paths and key state files are rejected. A mismatch
between project and Manager webhook references is also rejected; explicitly
reconcile the binding instead of silently redirecting the endpoint.

Public results filter known private paths, Bot/conversation identities and
loaded webhook keys/addresses, including dynamic object keys. Fixed protocol
field names remain intact; if a key occurs in a fixed field name, binding fails
rather than hiding a validation field or exposing the key. Request parameters
are also checked against the loaded webhook key.

Authorized code diffs, results and check output still return to the designated
Grok conversation. This is not a general secret scanner and cannot guarantee
filtering of unknown secrets in project content, check stdout or environment.
Use trusted projects and reviewed named commands; keep sensitive data out of
authorized files and output. Dedicated sessions and tool-file scope are not
security isolation between same-user processes or an OS sandbox.

## Local verification

```sh
uv run --locked --python 3.11 python -m unittest discover -s tests -p test_project_bridge.py -v
uv run --locked --python 3.11 python -m unittest discover -s tests -v
```

Project-entry tests use temporary Git projects, isolated Pi configuration and
fake hc, including subprocess CLI fixtures. They cover defaults and field
overrides, persistent recovery, duplicate-dispatch refusal, uncertain creation
and delivery, continuation, cleanup, notification identity/path checks,
configuration drift and private output. They do not call a model or real
network and do not establish a real Pi, Grok or Herdr App integration.
