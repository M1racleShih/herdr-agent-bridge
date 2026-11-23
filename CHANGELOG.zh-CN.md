# 更新日志

[English](CHANGELOG.md) · 简体中文

## 0.1.0

首个公开版本通过 herdr-core 把 Hermes Desktop 或 Grok Bot 对话接入本机的 Pi 或 Codex。一个请求可以连续执行多轮，并把经过核验的改动带回原对话。

- 绑定单一项目，或通过 Personal Agent 入口发现获授权的 Git 项目。新登记由可信操作者确认。
- 在原生 Agent 会话内追加需求、打断执行、人工接管，并在支持的情况下恢复原会话。
- 在接受开发任务前，检查 diff、改动范围和登记命令的退出码。
- 通过 Hermes 本机中继或 Grok HTTPS 例程回传结果。回复丢失后，可凭保存的请求和事件标识核对原任务。
- 用 uv 安装 Python CLI，无需模型即可运行本地 smoke。wheel 内含操作 skill 和配套参考文档。

支持的运行模式、原生命令、平台范围和 Pi 会话限制见[兼容范围](README.zh-CN.md#compatibility)。
