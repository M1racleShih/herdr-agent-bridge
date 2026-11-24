# 贡献指南

[English](CONTRIBUTING.md) · [安装与首次运行](docs/INSTALL.zh-CN.md)

修改源码前，请阅读 [AGENTS.md](AGENTS.md) 和相关产品文档。开发者可以选择自己的开发 harness；本仓库维护共享实现、测试和产品使用契约。

说明具体问题、修改后的行为及实际验证。将重构和无关清理分开；在本仓库更新受影响的产品文档。区分方案、fixture 结果和有真实集成证据的行为。

使用 [uv](https://docs.astral.sh/uv/getting-started/installation/)、Python 3.11 和供 JavaScript fixture 使用的 Node.js，安装可编辑项目和锁定的开发工具：

```sh
uv sync --locked --python 3.11
uv run --locked --python 3.11 python tools/check.py all
uv run --locked --python 3.11 python tools/check_install.py
```

`check.py all` 运行质量门禁和本地回归；`check_install.py` 构建 wheel 和 sdist、从 sdist 重建 wheel，并在源码目录外进行干净安装与回归。fixture 使用 fake hc、临时仓库、本地进程和回环 HTTP，不调用真实 Agent 或模型。只运行回归：

```sh
uv run --locked --python 3.11 python -m unittest discover -s tests -v
```

如需无模型地检查真实安装的 hc，请按[隔离 smoke 说明](docs/INSTALL.zh-CN.md#不调用模型的首次成功)操作。它创建专用 session；不要对用户的活动会话做实验。

普通问题请到 [GitHub Issues](https://github.com/M1racleShih/herdr-agent-bridge/issues) 提交最小复现、版本、预期与实际行为和脱敏证据。不要公开本机路径、原始对话或凭据。漏洞请通过 [SECURITY.md](SECURITY.zh-CN.md) 中的渠道私下报告。

## 贡献 coding-agent 适配

从现有的[能力声明](src/herdr_agent_bridge/agent_capabilities.py)、[适配代码](src/herdr_agent_bridge/agent_adapters.py)、[原生命令处理](src/herdr_agent_bridge/native_commands.py)和[开发契约](docs/DEVELOPMENT.zh-CN.md)入手。这些是协同工作的实现位置，并非稳定的插件 API：新 Agent 还需要按实际情况接入协议选择、原生证据和生命周期。实现前确定 Agent 版本、支持的运行模式、启动与恢复行为、模型选择、权限边界及原生命令。

使用合成且脱敏的原生历史 fixture 和 fake hc，按需扩展[适配测试](tests/test_agent_adapters.py)、证据测试和[原生命令契约测试](tests/test_native_commands.py)。覆盖精确提交的输入、session 和轮次身份、最终回答及完成顺序；对缺失、有歧义、损坏或不支持的证据安全拒绝。根据支持范围测试启动、追问、打断和恢复，并覆盖权限提示、普通输入冲突、重复操作 ID，以及未知的输入或通知送达。送达未知必须核对，不能重放。保留原 journal 格式、路径和身份，不能将终端 `done` 当作核验成功。

贡献说明应区分 fixture 结果和真实集成。要声明某项真实 Agent 支持，另行记录系统、hc、Agent 和模型版本，授权的环境，关联的真实原生 session／轮次证据，独立核验，失败或未知状态，以及专用资源的清理。本机运行器不能证明 Bot 派单或回传。只公开脱敏摘要；凭据、原始对话、私有项目文件、本机配置和运行产物不得提交到 Git。让支持文档与证据一致，明确标示未验证功能。

## 真实交互 Codex 验收

本机 bridge 和真实 Agent 验收需要 **Python 3.11+**、`hc`、`codex` 及可用的现有 Codex 认证／provider 配置。只读测试曾在 Linux 的 hc 0.1.22、Codex CLI 0.171.0 通过。真实检查使用模型；上面的单元测试不使用模型。

```sh
herdr-bridge tool accept_codex
```

该命令在私有 hc session 以 `--no-daemon`、`read-only` 和 `on-request` 权限启动交互式 Codex，独立检查源码事实与引用，在同一原生线程追问并处理澄清，再检查超时、送达未知及 hc 控制情况。它仅在本机丢弃一次真实 send 回复，并非真实网络中断；澄清答案由脚本提供，不是真人。控制程序是 fixture，不是额外 Codex 运行。不会自动批准权限或信任提示。

在 Codex 可以启动自身 sandbox 的环境中运行。嵌套 sandbox 可能隐藏共享 socket 目录并导致 `blocked`；这是需要检查的失败，不构成放宽内层 sandbox 的许可。

随机 session 名称、恢复目录和任务意图在副作用之前写入。每次运行将私有证据写到 `.artifacts/acceptance/codex/<run>/`：`acceptance.json` 保存检查与结果，`calls.json` 保存尝试，`bridge/journal.json` 保存任务、终端和原生轮次映射；清理前会保存专用 run 的 `codex-session-*.jsonl` 私有副本。独立 Codex home 和 XDG 目录是临时的，只复用 provider 配置、认证和对该准确项目已有的信任，不导入 hooks、插件、MCP server 或无关项目的信任。这是状态隔离，不是防御同一用户进程的隔离边界。成功清理会删除含复制认证的临时 home；如果清理失败，保留 journal 并核对原终端后再清理，恢复目录可能仍含认证。

也可以手动驱动最小 bridge：

```sh
herdr-bridge agent --run .artifacts/manual-run start --project "$PWD"
herdr-bridge agent --run .artifacts/manual-run observe --wait 20
herdr-bridge agent --run .artifacts/manual-run submit analysis-1 --prompt-file /tmp/task.txt
herdr-bridge agent --run .artifacts/manual-run observe --task-id analysis-1 --wait 20
herdr-bridge agent --run .artifacts/manual-run submit followup-1 --parent analysis-1 --prompt-file /tmp/followup.txt
herdr-bridge agent --run .artifacts/manual-run close
```

提交前等待就绪；追问前检查首个任务，直到有相关联的完成证据。送达未解决时，复用任务 ID 和新 prompt 都会被拒绝。`needs_human` 可能是 Agent 问题或原生权限对话框，工具不会替人回答。终端状态和返回证据不会设置 `verified_success`；调用方必须像验收程序一样通过 `Bridge.verify` 独立核验。此适配器是本机单写入者接口，不提供 Grok 传输、通知队列或自动恢复／重放。

可选：从**通过**的验收记录生成中文精简 HTML 报告：

```sh
herdr-bridge tool render_codex_report .artifacts/acceptance/codex/<run>/acceptance.json \
  --output .artifacts/acceptance/codex/report.html --regression-count 34
```

HTML 含脱敏摘要及私有记录的 SHA-256；原始对话、provider 配置和本机路径不得公开。渲染器也接受失败记录，但必须标为未通过。

## Grok 管理边界

`herdr-bridge manager probe` 可检查隔离的本机就绪情况；配合上述 unittest 回归，两者均不使用模型或用户 session。Manager 将本机 Agent bridge 与固定 Bot／对话绑定、精简证据、确定性验收契约和持久的官方 routine webhook outbox 组合。命令、私有凭据格式、观察器生命周期及恢复见[操作契约](docs/GROK.zh-CN.md)。

hc 0.1.18 的首次 `idle` 可能早于可输入状态。Manager 使用终端创建时间等待该版本的 10 秒启动保护并再次检查实时状态；提交前必须有 `input_ready=true`。

测试使用 fake hc、临时 journal 和回环 HTTP fixture。本机真实 Codex 检查也不能证明 Grok 发起派单或收到结果。Grok 集成验收需要真实 Grok 对话／本机工具证据、关联源码核验、实际问答续轮、webhook 和收据关联，以及专用 core、复制的临时认证和观察器进程的清理。原始记录保存在 `.artifacts/`。

## Pi 集成

使用 `herdr-bridge manager probe --agent pi` 和相同的本地 unittest。Pi 解析器、bridge、SDK 启动器测试使用临时原生历史 fixture、fake hc 和合成 SDK 包，不访问真实认证或模型。SDK fixture 测试调用 Node.js，未安装时跳过；网络 fixture 替换 Pi SDK 相对位置的公共 undici 依赖，检查代理／超时和 dispatcher 清理，不访问网络。

`herdr-bridge tool accept_pi` 会调用配置的真实模型，使用专用 hc session、设置、缓存和原生历史。API key 只为选定 provider 复制；OAuth 使用 Pi 原生认证文件及正常锁／刷新语义，因此其持久刷新需要授权。切勿在一次性副本中刷新 OAuth 后删除替换 token。测试拒绝信任、禁用可执行资源，仅开放 read/grep/find/ls；这些工具选择不是 OS sandbox。

真实本机检查覆盖四个确定性轮次、源码／引用核验、丢弃 send 回复后不重放的核对，以及清理。语言答案由脚本预设，不调用 Grok。成功或失败时，清理前都会尝试保存该 run 的原生历史，不将认证复制到证据目录。见 [Pi 操作与证据边界](docs/PI.zh-CN.md)。

## 可复现质量门禁

使用 Python 3.11 和锁文件中的开发工具：

```sh
uv sync --locked --python 3.11
uv run --locked --python 3.11 python tools/check.py all
```

CI 通过同一 `tools/check.py` 入口分别运行 `lint`、`format`、`types`、`naming` 和 `test`。Ruff 检查 Python 代码的 E4/E7/E9/F 和导入顺序，格式化为独立门禁。回归在 Python 3.11 和 3.15、Node 24 上运行，并通过 unittest 包含现有 JS fixture。

Mypy 当前检查 `bridge_common`、`bridge_protocol` 和 `development_task`，正常跟随导入并检查无类型函数内部。持久化和 hc 传输要求完整签名并拒绝未参数化泛型；策略选择及核验票据约束模型和状态边界。动态 journal 字典、大部分状态方法、适配器和 CLI 模块仍是渐进类型工作，并非全面 strict 覆盖；不使用忽略缺失导入或跳过导入的替代方式。

`naming` 门禁检查文件名和文本中的开发阶段名称。精确历史输入例外及理由记录在 `tools/naming_exceptions.json`；新公共别名或输出路径不能列为例外。协议状态字段 `phase` 和上游版本号不变。

## 将发布包上传至 PyPI

GitHub Release 发布并附上 wheel、sdist 和 `SHA256SUMS` 后，在默认分支手动运行 **Publish a GitHub release to PyPI**，输入该版本的 tag。工作流先校验两份附件，再通过 PyPI Trusted Publishing 上传原文件。在 PyPI 为本仓库绑定 GitHub 发布者时，工作流文件填 `publish-pypi.yml`，环境填 `pypi`。
