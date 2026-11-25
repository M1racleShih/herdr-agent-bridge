# 安装与首次运行

[English](INSTALL.md) · [README](../README.zh-CN.md) · [操作指南](PROJECT.zh-CN.md)

Bridge 的**必需运行时**是 [herdr-core](https://github.com/herdrdev/herdr-core#install)。请在运行 Bridge 和编码会话的机器上安装它。Bridge 通过 PATH 查找并调用它提供的 `hc`，用来控制本机终端。Linux 集成实测使用 `hc 0.1.22`。Bridge 本身需要 Python 3.11+，Python 运行时仅使用标准库。无模型 smoke 支持 Linux 和 macOS，详见[支持矩阵](../README.zh-CN.md#compatibility)。

使用 Pi 开发还需要 [Node.js](https://nodejs.org/en/download) 和 [Pi](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)；使用 Codex 开发需要 [Codex CLI](https://github.com/openai/codex)。两者都需要可用的模型访问和原生认证。无模型 smoke 不需要它们。

## 请 Agent 安装

可以把以下指令交给能够访问本机的 Agent：

> 请按照 https://github.com/M1racleShih/herdr-agent-bridge/blob/main/docs/INSTALL.zh-CN.md 安装 herdr-agent-bridge 0.1.1。先检查已有安装，再运行无模型 smoke。登录、产生模型费用、更改全局配置或 skills、使用已有会话之前请先征得我的授权；未经授权不要启动模型任务。

以下命令安装 0.1.1。随包提供的 Agent skill 可以按需安装。

## 检查环境

先准备好 [uv](https://docs.astral.sh/uv/getting-started/installation/)，它可以获取 Python 3.11。检查本机工具：

```sh
uv --version
hc --version
command -v herdr-bridge || true
uv tool list
```

如果找不到 `hc`，Linux 和 macOS 可使用上游提供的安装命令：

```sh
curl -fsSL https://releases.herdr.dev/core/install.sh | sh
hc --version
```

如果已有 `herdr-bridge`，检查其路径和 `herdr-bridge --version`。替换安装前先核对活动 run：后台观察器在清理前仍使用原来的 Python 环境。决定如何保留其他安装后再覆盖。

## 安装 0.1.1

使用 uv 从 [PyPI](https://pypi.org/project/herdr-agent-bridge/0.1.1/) 安装固定版本。

```sh
uv tool install --python 3.11 'herdr-agent-bridge==0.1.1'
export PATH="$(uv tool dir --bin):$PATH"
herdr-bridge --version
herdr-bridge --help
```

版本命令应输出 `herdr-bridge 0.1.1`。若找不到命令，将 `uv tool dir --bin` 输出的目录加入当前 `PATH` 后重试；`uv tool update-shell` 会修改 shell 配置，仅在获得授权后使用。也可以安装已核验的 0.1.1 wheel：`uv tool install --python 3.11 /path/to/herdr_agent_bridge-0.1.1-py3-none-any.whl`。

## 不调用模型的首次成功

创建专用临时目录并在其中执行；报告默认写在该目录下的 `.artifacts/hc-smoke/results.json`：

```sh
HAB_CALLER_DIR="$PWD"
HAB_SMOKE_DIR="$(mktemp -d)"
cd "$HAB_SMOKE_DIR"
herdr-bridge tool smoke_hc
```

命令使用已安装的 `hc`、随机专用 session、隔离的 XDG 目录和不调用模型的本地 fixture，不访问外部服务。成功时退出码为 0，JSON 中有 `"passed": true`、三项通过的检查和报告路径。成功清理后，专用终端、server 和临时目录会被移除，报告保留。这只证明 hc 基线，不代表真实 coding agent 或集成验收通过。

如果失败或中断，先检查报告中的 `error`、`session`、`scratch` 和 `cleanup` 字段，再处理报告标识的专用 session 和临时目录；不要停止无关 session，也不要盲目重试送达未知的操作。报告可能包含本机路径，应私下保存。成功后如不再需要报告，可在同一个 shell 中删除专用首次运行目录：

```sh
cd "$HAB_CALLER_DIR"
rm "$HAB_SMOKE_DIR/.artifacts/hc-smoke/results.json"
rmdir "$HAB_SMOKE_DIR/.artifacts/hc-smoke" "$HAB_SMOKE_DIR/.artifacts" "$HAB_SMOKE_DIR"
```

该安装的所有 run 和观察器关闭后，可以用 `uv tool uninstall herdr-agent-bridge` 移除 uv 工具安装。此命令不会移除 hc、项目配置、journal 或另行复制的 skill。

## 可选：真实任务

明确获得项目、Agent、模型访问和可能费用的授权后，按[项目指南](PROJECT.zh-CN.md#启动同任务继续与结束)创建私有项目绑定和一个小型开发 contract，启动选定的原生 Pi 或 Codex，提交一次任务并独立核验结果。权限和信任提示由人处理。结束任务时记录明确决定并关闭专用 run；保留恢复所需的 journal 和证据。超时或丢失回复意味着送达未知，先核对原请求，不盲目重建或重发任务。隔离的模型验收及清理方式见[贡献指南](../CONTRIBUTING.zh-CN.md)。

## CLI 与升级契约

固定项目使用 `herdr-bridge --config /private/project.json COMMAND`；多项目绑定使用[通用 Personal Agent 入口](PROJECT.zh-CN.md#通用-personal-agent-入口)。完整参数见 `herdr-bridge --help` 和各子命令 `--help`。使用安装包所在的 Python 环境运行 `env -u PYTHONPATH -u PYTHONHOME python -P -m herdr_agent_bridge` 也是同一入口。底层接口是 `herdr-bridge manager`、`herdr-bridge agent` 和 `herdr-bridge app`；`herdr-bridge tool NAME` 选择开发或验收程序。验收程序从当前目录读取所需的明确源码输入，应在准备了这些输入的专用验收项目中执行。普通项目操作不需要源码 checkout 或 harness。

项目命令成功时 stdout 输出一个 JSON 文档；操作错误退出 1，安全诊断 JSON 写到 stderr；参数错误退出 2，argparse 说明写到 stderr。独立核验失败退出 1；版本和帮助为文本，退出 0。底层兼容命令保留各自输出和退出契约。start、submit、recover 和未知通知投递不自动重试；请求、轮次、原生会话、事件身份及原因码维持原语义。

wheel/sdist 包含 MJS 和协议 JSON。正常 wheel 安装会解包资源，供 Node 与其相邻导入使用；不支持直接从 zip 执行 Node 资源。后台观察器使用当前解释器经 `-P -m herdr_agent_bridge.managed_watch` 或 `-P -m herdr_agent_bridge.grok_bridge` 启动；必须保留该安装环境，直到 run 的观察器和 Agent 清理完毕。子进程清除 `PYTHONPATH` 和 `PYTHONHOME`，防止当前目录遮蔽，同时保留安装环境和 user site，包括已设置的 `PYTHONUSERBASE`。

旧配置、journal、outbox 与任务指针原位读取，缺失新字段仍沿用既有默认规则。迁移无需重建请求。源码 `scripts/*.py` 保留薄入口，要求同一 Python 环境已安装本包；按职责命名的脚本与 `project_entry.run_bound` 导入仍受支持。相邻 MJS/JSON 是私有包资源，不是公共脚本 API。新固定包装器应导入 `herdr_agent_bridge.project_entry.run_bound` 并继续绑定原私有配置。不要将 `src` 加入 `PYTHONPATH`，也不要因切换启动命令而重建任务。

旧轮次的源码核验由持久化原 prompt 选择旧或新来源，并检查原路径的实际文本和 AST；答案不能自行切换来源。历史报告仍可读取，但只有被引用的原始源码仍匹配时核验才能通过；不能用安装包内容冒充已变化或变薄包装的历史原路径。

<a id="runtime-and-acceptance-directories"></a>
## 运行与验收目录

新 Manager run 位于项目的 `.artifacts/runs/<run-id>/`。固定项目入口自动生成该指针；底层 `manager start --run` 只接受此目录下的新专用 run。项目绑定保持单层安全名称；通知必须匹配原项目、owner、run 和事件身份。

验收证据按用途放在 `.artifacts/acceptance/codex/<id>/`、`.artifacts/acceptance/pi/<id>/` 和 `.artifacts/acceptance/core-features/<id>/`。各目录包含 `acceptance.json` 和相关调用日志、快照。Pi 和核心能力报告通过 `run` 关联独立 Manager 目录；`accept_pi --run` 指定 Manager run，`accept_codex --output` 可指定新的证据目录。

已有 `.artifacts/phase2/` run 与其配置、journal、outbox 原位继续使用；新建 run
不能选择该目录。读取不会迁移记录或重建任务；恢复、同任务继续和关闭仍
使用原身份。历史 `.artifacts/phase1/`、`.artifacts/phase5/` 验收报告保留原位，
按原路径读取；当前 Codex 渲染器可以处理旧 Codex 报告，不重写证据。

新隔离 App 终端使用 `herdr-bridge-app`，名称在启动前写入 `app.json`；缺少名称
字段的历史记录仅按 `phase3-app` 核对原终端。清理还必须匹配保存的终端地址
及 App 执行参数。原生 App 保持同窗口关联与接管契约。

公开 Codex 验收命令为 `herdr-bridge tool accept_codex` 和 `herdr-bridge tool render_codex_report`。可选渲染器输出中文 HTML。已移除的开发阶段模块、脚本和命令没有公共别名；调用方应更新命令，不改变原 run、请求或事件身份。

## 可选产品 skill

随包提供的 `skills/herdr-agent-bridge/` 适用于 CLI 0.1.x。可以从源码复制完整目录，或使用已安装分发包的 Python 解释器定位：

```sh
/path/to/installed/python -c 'from importlib.metadata import distribution; d=distribution("herdr-agent-bridge"); print(next(d.locate_file(p).resolve().parent for p in d.files if str(p).endswith("share/herdr-agent-bridge/skills/herdr-agent-bridge/SKILL.md")))'
```

仅在获得授权后，将完整目录复制到目标 Agent 支持的 skill 位置，并按宿主要求刷新发现。分别核对安装、发现和实际 Bot／工具调用。该 skill 不授予额外的访问、权限批准、提交或部署权。
