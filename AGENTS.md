# Working in this source repository

- Start with `README.md` and the usage documentation relevant to the change. Read code, contracts and tests as evidence requires it.
- These rules apply to the shared source, regardless of the developer's harness. Keep development plans, research, progress records and personal skills in the developer's chosen harness; product implementation, tests and usage documentation belong here.
- Inspect the working tree before editing, preserve others' changes, and make the smallest change that fulfills the task. Follow the existing module and CLI contracts.
- The initial execution backend is herdr-core (`hc`). Herdr TUI and Herdr App are separate integration surfaces.
- Prefer an existing maintained component when it meets the task contract. Record limitations and verification before adopting it.
- Preserve the distinction between submitted input, observed terminal state, returned evidence, and verified task success.
- Treat a timeout or lost reply as uncertain delivery until reconciled. Do not blindly replay task creation or prompts.
- Use dedicated test sessions and scratch XDG configuration/state/runtime paths. Do not inspect, stop, or modify a user's unrelated sessions.
- Never silently approve a coding agent's permission or trust dialog.
- Match checks to changed behavior. Do not claim real-agent or Grok integration based on fixture tests.
- Keep credentials, private project contents, raw local transcripts, and machine-specific configuration out of commits. Generated artifacts live in .artifacts/.
- Use `uv sync --locked --python 3.11` for development and `uv run --locked --python 3.11 python tools/check.py test` for product regressions. These tests use fake backends and disposable local fixtures; some require Node.js. The real smoke and model-backed acceptance commands are documented in `CONTRIBUTING.md` and have separate prerequisites and effects.
- Keep the source independently usable and testable. Product code, prerequisite checks and acceptance programs must not require an adjacent harness, its roadmap, its decisions or its skills.
- Preserve runtime directory contracts and existing `.artifacts/` journals, notification state and raw evidence. A repository cleanup does not authorize moving or rewriting runtime state.
- For architecture or operational changes, update the affected product usage documentation and return actual verification and limitations to the caller's development workflow. Do not mark unfinished work or human acceptance complete.
- For delegation, assign bounded ownership, preserve concurrent edits, and inspect the resulting artifacts and verification.
