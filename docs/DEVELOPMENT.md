# Development task interface

English | [中文](DEVELOPMENT.zh-CN.md)

For daily native Pi and Codex use the [fixed project entry](PROJECT.md), which
retains normal agent configuration, tools and App. This page describes the
lower-level development contract and takeover interface. The SDK's restricted
tools and private App configuration below apply to `isolated` mode, not the
resource policy of daily native mode.

At the native fixed project entry run
`start REQUEST_ID --agent codex --development-contract FILE`, or select
`--agent pi`. Its contract fixes the goal, `task_id`, `allowed_paths`,
`acceptance` and a `checks` list of names configured for the project. The
entry expands those names to the registered `name`, `argv` and `timeout`
definitions. A project allows one unfinished development run, and a new run
also requires the prior one cleaned up. An entry without a contract remains
read-only.

In native development rounds, the task instructions, project rules and available
tools govern subagent use and Git commits. The bridge tracks the main session
and verifies changes against the task's worktree baseline.

At the fixed project entry submit a round with
`submit <round-id> --prompt-file <file>` and use
`--parent <latest-round-id>` for later rounds. The lower-level
`herdr-bridge manager` interface instead takes a contract containing the full
check definitions and submits with
`submit <round-id> --development-task <task-id> --prompt-file <file>`.
Its `submit ... --operator` only prepares and records App input; it never
sends. These manager-only flags do not belong to fixed project commands.

`takeover --actor <operator>` persistently pauses automatic input. After the
operator enters the prepared text, observe the native answer using the
relevant entry (`status --observe` for the fixed entry, `observe` for
manager). `reconcile` obtains current file/session fingerprints, and
`resume --fingerprint <sha> --review <note>` resumes.

The fixed entry's `verify` and manager's `verify-development` independently
gather the baseline diff, check commands and exit codes, and check that source
did not change during checks and that no out-of-scope changes occurred.
`decide accept|reject|terminate --actor <operator> --note <note>` records the
acceptance decision separately. Acceptance requires the last verification
still to cover current code and session; passing checks alone does not mean a
person accepted the result.

The Pi development SDK adds only `scoped_edit`, `scoped_write` for specified
files and preauthorized `run_check`; it does not expose arbitrary bash.
Named checks still execute project code and are not an OS sandbox. Read-only
tool and shared native OAuth boundaries continue to apply.

`herdr-bridge app --run <run> start [--display <private-display>]` starts a
separate App window in the same private core. Then `bind` displays the
executing agent, `status` saves the mapping and `shot --output <png>` takes
a screenshot. `close` this App before cleaning the bridge. Reconcile an
uncertain launch/bind receipt from the original record before any retry. App
configuration, sessions and test display are separate from the user's everyday
window.

## Adding an agent adapter

Register the agent's development runtime modes and explicit native commands in
[agent capabilities](../src/herdr_agent_bridge/agent_capabilities.py). The
fixed entry and Bridge check this declaration before reserving a development
slot. [Agent adapters](../src/herdr_agent_bridge/agent_adapters.py) provide
startup arguments and model selection. Each new command also needs pre-send
checks and native-state readback in
[native command handling](../src/herdr_agent_bridge/native_commands.py).
Verify retries with the same operation ID, conflicts with ordinary input and
unknown delivery using the
[shared command contract tests](../tests/test_native_commands.py).
