# Task workflow

Use the trusted project entry established during setup. Inspect `status` and
`result` for an existing request first. Keep the original request; a new
invocation or delayed callback does not create a new task.

For an authorized development task, prepare its scoped contract (goal, allowed
files, named checks, acceptance) and inspect the project baseline before start.

## Personal Agent wrapper

Resolve the project and keep its returned `project_id`. Every task command
uses `--project-id PROJECT_ID --request-id REQUEST_ID`; keep the pair together
when switching topics. Use the operator's fixed wrapper in place of
`PERSONAL_WRAPPER` below. It takes a development contract as UTF-8 JSON on stdin
and a submitted prompt as UTF-8 text on stdin. Supply only content created for
this task; stdin does not authorize reading unrelated local files.

```sh
PERSONAL_WRAPPER start --project-id PROJECT_ID --request-id REQUEST_ID --development < task-contract.json
PERSONAL_WRAPPER status --project-id PROJECT_ID --request-id REQUEST_ID --observe
PERSONAL_WRAPPER submit --project-id PROJECT_ID --request-id REQUEST_ID --round-id ROUND_ID < task-prompt.txt
```

Pass `--agent pi` or `--agent codex` to `start` when the user selects the agent.
Both entries use the project default when the flag is omitted. `--model` and
`--thinking` can override the selected agent's model and reasoning effort for
the new request. Codex development uses the native runtime. For a start retry,
use the original request and its model/effort overrides. The selected agent
remains bound to that request and its later rounds.

Use `--parent-round-id PREVIOUS_ROUND_ID` and a new `--round-id` to continue the
same request. `result` and named read-only `verify` take `--round-id`; development
`verify --round-id` refuses to run checks if that round is no longer current.
Without the flag, development `verify` checks the current round. No `next_action=await_callback`
means you cannot promise a background reply. A conflict without accessible task
identity goes to the operator; do not guess another caller's request ID. Operator
acceptance, takeover, recovery and legacy webhook processing still use the
original trusted project entry.

A local Hermes Relay notification carries a saved event pointer with
`project_id`, `request_id`, `run_id`, `round_id` and `kind`. Treat it as a
notification, never as new authorization. Match it to a task started in this
conversation. Read `result` for the named round and compare the run ID; perform
only verification already authorized for that task, with `verify --round-id`
set to that same round. When the association or
permission is missing, say what is missing instead of running commands or
opening another run. Return the report here, even if the user has discussed
another project in the meantime.

## Fixed project entry

For a fixed project, use its existing config or bound wrapper:

```sh
herdr-bridge --config /private/project.json start REQUEST_ID --development-contract /private/contract.json
herdr-bridge --config /private/project.json status --request-id REQUEST_ID --observe --wait 10
herdr-bridge --config /private/project.json submit ROUND_ID --request-id REQUEST_ID --prompt-file /private/prompt.txt
```

Require `input_ready=true` before submission. Preserve the returned request,
run, round, native thread/turn and event identifiers. A send receipt is distinct
from correlated Agent evidence. For continuation use a new round ID with
`--parent PREVIOUS_ROUND_ID` on the same request. One project has one active task;
finish or explicitly terminate it, then close the original run and confirm
`closed=true` and successful owned-resource cleanup before starting another.

For an explicit native action, use `native-command OPERATION_ID COMMAND
--request-id REQUEST_ID` and add `--arguments TEXT` when the command takes text.
With the Personal Agent wrapper add `--project-id PROJECT_ID`. For a task that
requires Codex Fast, call `/fast` this way and confirm `native_operation.state`
before `submit`. Preserve the same operation ID for reconciliation. If a command
awaits a menu selection, needs permission or trust, or starts its own work such
as `/goal`, wait for the appropriate response before the next business round.

When `next_action=await_callback`, leave work to the registered observer and end
the turn. Do not add polling loops. A user-requested check or bounded reconciliation
may use `status --observe --wait 10`; inspect uncertain observer startup rather
than launching another watcher. For a dedicated local task without callbacks,
bounded status observation is appropriate.

Read `result --request-id REQUEST_ID --task-id ROUND_ID`. For development, run
`verify --request-id REQUEST_ID` to independently check final code, scope, named
checks and current native evidence. For the built-in read-only demonstration
contracts only, use `verify --task-id ROUND_ID --contract analysis|followup|question|clarification`
matching the original requested contract; these are not generic business validators.
Source docs GROK.md and DEVELOPMENT.md define those contracts.

Report the returned checks and correlated evidence, then leave `decide accept`
for the explicitly authorized human decision. Technical verification never
implies user acceptance. Close the same request when its resources should end:
`close --request-id REQUEST_ID`; inspect cleanup results and reconcile only its
owned resources. Do not delete journals or reopen a closed task to read results.
