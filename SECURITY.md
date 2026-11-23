# Security

[简体中文](SECURITY.zh-CN.md) · [Installation](docs/INSTALL.md)

## Report a vulnerability privately

Sign in to GitHub and use [private vulnerability reporting](https://github.com/M1racleShih/herdr-agent-bridge/security/advisories/new) for **M1racleShih/herdr-agent-bridge**, or select “Report a vulnerability” on its Security → Advisories page. Do not put vulnerability details, working exploits, credentials or private project data in a public issue or pull request. If private reporting is unavailable, contact the maintainers through an existing private channel; if none is available, open an issue asking only for a private security contact, without describing the vulnerability.

Include the affected bridge version and commit if known, operating system, hc and relevant agent versions, prerequisites, a minimal reproduction with synthetic data, expected and observed behavior, and the likely impact. Identify any uncertainty. Do not send real tokens, raw conversations, private source files or full runtime journals. A maintainer can coordinate further disclosure and a fix through the private report; there is no promised response time or disclosure schedule.

Reports for the current 0.1.0 release are in scope. Third-party vulnerabilities should also be reported to the relevant upstream project using its own security process.

## Operating boundaries

The bridge runs local programs and can start coding agents with access determined by their own runtime, the project and the invoking user's permissions. Native agent settings and tools may have broader access than isolated acceptance fixtures. Review a task, project, agent, model and named checks before authorizing execution; named checks run project code and path restrictions are not an OS sandbox. Handle permission and trust dialogs as a human operator.

Keep project bindings, authentication, webhook credentials, journals, native session histories and notification state private. The tools' temporary state separation is not a security boundary against other processes running as the same user. Preserve and inspect original records after a timeout or lost reply; uncertain task or notification delivery must be reconciled before a retry. The [project guide](docs/PROJECT.md) describes operation and recovery, and [Contributing](CONTRIBUTING.md) separates simulated tests from real acceptance.
