# Changelog

English · [简体中文](CHANGELOG.zh-CN.md)

## 0.1.1

Development prompts now leave subagent use and Git commits to the task instructions, project rules and available tools. The bridge continues to track the main session and verify changes against the starting worktree.

## 0.1.0

The first public release connects Hermes Desktop or Grok Bot to a local Pi or Codex session through herdr-core. A request can continue across turns and return verified changes to its original conversation.

- Bind a single project or discover authorized Git projects with the Personal Agent entry. A trusted operator confirms new registrations.
- Keep the same native agent session for follow-ups, interrupts, human takeover and supported recovery.
- Check the diff, file scope and exit status of named commands before accepting a development task.
- Deliver result events through the local Hermes relay or a Grok HTTPS routine. Saved request and event IDs support reconciliation after a lost reply.
- Install the Python CLI with uv and run a local smoke without a model. The wheel includes the operating skill and its references.

See [compatibility](README.md#compatibility) for supported runtimes, native commands, platform coverage and Pi history limits.
