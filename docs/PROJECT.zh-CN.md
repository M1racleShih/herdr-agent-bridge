# 固定项目入口

[English](PROJECT.md) | 中文

`herdr-bridge` 为一个项目提供持久入口，复用现有 `Manager`、run、
原生 Agent 会话、开发任务登记和通知 outbox。项目配置只保存身份、执行策略、命名
检查及原 run 指针，不维护第二套任务状态机。

本页描述本地接口和恢复约束。本地 fixture 测试不证明真实 Bot、Agent 或 App
已经接通；实际接入需要单独核对。Bot 与 routine 的规则见[指令模板](PROJECT_BOT_TEMPLATE.zh-CN.md)。

## 首次绑定

需要 Python 3.11+、可用的 hc 和所选 Agent 的 CLI 及原生认证。日常入口默认运行于
`native`，在正常 core 中创建该任务的终端，沿用 Agent 的全局和项目资源、权限及信任行为。
Pi 会话保存在 run 的专用目录；Codex 将原生记录留在自己的会话目录，run 保存准确的
线程绑定。核对允许修改的文件、
检查命令和本地合成数据边界。`init --runtime isolated` 仅用于专用验收。

旧配置保持原运行方式。执行 `runtime native` 只改变后续新请求，旧请求仍关联原 run。
配置 `app-space SPACE_ID` 后，新建原生请求会自动关联既有 App 并绑定指定空间。
也可在初始化时传 `init --app-space SPACE_ID`；旧配置未设置时保持手动绑定：
`herdr-bridge app --run RUN start`，再执行 `bind --space SPACE_ID`。
不创建第二个 App，也不关闭用户空间或共享 core。

先在本机保存命名检查文件。例如 `checks.json`：

```json
[
  {"name": "unit", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"], "timeout": 120}
]
```

检查名须唯一；`argv` 是完整命令参数数组，每条检查限时 1–120 秒。检查会执行
项目代码，必须先检查这些命令的实际范围。依赖安装、长期预览和云端写入不应
作为此类短时检查隐式加入。

固定入口的命令前缀为：

```sh
herdr-bridge --config /private/path/project.json
```

使用完整命令执行一次初始化：

```sh
herdr-bridge --config /private/path/project.json init \
  --project-id sample-app --project /absolute/path/to/app \
  --bot-id PROJECT_BOT_ID --conversation-id PROJECT_CONVERSATION_ID \
  --webhook-config /private/path/webhook.json --checks-file /private/path/checks.json
```

这些路径和身份均为占位符。实际文件只存本机私有位置或忽略的 `.artifacts/`。
项目根目录会规范化，项目配置保存为 `0600`，已有配置不能被 `init` 覆盖。
webhook 配置沿用[Grok 管理接口](GROK.zh-CN.md)，包含 HTTPS endpoint、key 及匹配的
Bot／对话身份；项目配置只保存它的引用。初始绑定也可以省略 webhook。

以下是无凭据的项目配置结构模板，通常应由 `init` 生成，不手写运行记录：

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

## 给 Bot 的通用适配脚本

可在本机私有位置放置薄包装脚本，固定项目配置并转发全部项目命令：

```python
from pathlib import Path
from herdr_agent_bridge.project_entry import run_bound

raise SystemExit(run_bound(Path('/private/path/project.json'),
                          audit_dir=Path('/private/path/entry-calls')))
```

此时 Bot 使用 `python3 /private/path/project-command.py` 作为固定前缀。
适配器不内置任务 ID、轮次或 prompt，调用方按用户需求提供参数；它拒绝
`--config` 覆盖，项目状态仍由同一个 `project_bridge` 管理。换项目时建立另一份
配置和薄包装脚本，不复制任务逻辑。更新脚本无需重新建立已有 webhook。

调用审计仅记录操作名、时间、进程来源和返回状态，不保存参数、prompt、输出或
配置内容。审计写入失败时，执行前返回未尝试，执行后返回投递未知；后者不能
通过重发业务输入补救。审计不是调用方认证或不可篡改凭证。

`app-space SPACE_ID` 仅影响后续新请求。每个新请求保存自己的空间选择；重复
`start` 和 `status` 只读取已保存的 App 关联，不重复启动或绑定。
返回的 `app.evidence=saved_app_state` 不证明当前窗口仍可见。App 不可用或绑定
未知会返回原 run 身份及 `pending/error`，应核对原会话，不能另起 Agent。

## 本机接口：项目发现与确认登记

通用入口接入前，可以在本机用 `ProjectRegistry` 查询私有登记和授权目录。登记文件路径
与 `allowed_roots` 由本机可信操作者提供；传入无符号链接的绝对路径。不同调用方可以
共用登记文件、分别提供自己当次获授权的目录。登记文件不保存调用方权限或任务状态。

```python
from herdr_agent_bridge.project_registry import ProjectRegistry

registry = ProjectRegistry("/private/projects.json", allowed_roots=["/path/to/work"])
choice = registry.resolve("report-app")
```

`resolve` 优先按登记名称（不区分大小写）或项目 ID 查找当前仍有权访问的目录：
`registered` 可直接选用；`ambiguous` 要求指定一个匹配；`not_found` 表示在扫描范围内
没找到；`incomplete` 表示扫描遇到上限或目录不可用，不能据此声称只有一个候选。
未登记的唯一候选返回 `needs_confirmation` 和供用户核对的完整根目录。查询不写登记，
也不启动任何任务。

`discover()` 返回候选、`.git` 识别依据、登记 ID（未登记则为空）及扫描覆盖范围。
默认从授权根向下最多 4 层，最多返回 100 个候选、检查 10,000 个目录项。扫描到项目根
后不继续搜索其子目录，略过隐藏目录、常见依赖、虚拟环境和构建目录；不跟随符号链接，
只检查 `.git` 是目录还是常规文件，不读取文件内容。

操作者在用户确认了**具体目录**后，才调用 `registry.register_confirmed("/path/to/work/report-app")`。
登记时重新检查路径和权限；重复登记同一根目录返回原 ID，非 Git 项目也可按精确路径
显式登记。登记只建立项目身份，不授权执行代码或修改任何任务。

## 通用 Personal Agent 入口

本机操作者为一个调用方及其对话准备私有 `0600` JSON，例如：

```json
{
  "format": 1,
  "registry": "/private/projects.json",
  "allowed_roots": ["/path/to/work"],
  "owner": {"bot_id": "BOT_ID", "conversation_id": "CONVERSATION_ID"},
  "projects": {"REGISTERED_PROJECT_ID": "/private/existing-project.json"}
}
```

`registry` 可以由不同入口共用；每个入口单独设置授权目录、对话和执行绑定。`projects`
可先留空。确认用户选中的具体目录后，操作者可运行以下命令（也支持 `--name`）：

```sh
herdr-bridge personal --binding /private/caller.json register /path/to/work/report-app
```

已配置 Hermes Desktop Bot Chat 的用户可用下文的 `/hab-register` 确认卡完成登记。
其他对话平台仍使用可信操作者或自行接入经认证的确认接收器。不要把
`personal_entry.register_confirmed`、操作者命令或可更换绑定的入口作为 Agent 工具。

登记后，由操作者审查项目执行配置和命名检查，把**返回的登记 ID**映射到既有配置，
并[启用共享占位](#本机部署让多个绑定共用项目占位)后才允许启动新任务。既有配置的根和
Bot／对话必须与登记和当前调用方一致；旧请求、run、owner 和 webhook 无需改写。仅登记
或发现项目不授予执行权。`project_not_bound` 表示仍缺执行绑定；`shared_start_required`
表示操作者尚未启用共享占位。

给 Agent 的薄包装器只固定绑定，不需要为每个项目建立脚本：

```python
from herdr_agent_bridge.personal_entry import run_bound

raise SystemExit(run_bound("/private/caller.json"))
```

用该包装器的 `resolve NAME_OR_ID` 选择项目，也可用 `discover` 查看候选。任务命令
`start`、`status`、`result`、`submit`、`native-command`、`verify`、`close` 需要显式
`--project-id` 和 `--request-id`。`start` 可传 `--agent pi|codex`。
`start --development` 从标准输入读开发契约 JSON；`submit --round-id`
从标准输入读提示文本，两者均为最多 64 KiB 的 UTF-8，不接收任意输入文件路径。续轮用
新 `--round-id` 和 `--parent-round-id`；查看指定轮次用 `status/result --round-id`，
`--observe` 是有界观察。开发核验使用 `verify`；处理完成事件时加 `--round-id` 固定
预期轮次，如果已追加新轮就拒绝执行检查，不把新轮的核验报成旧轮。预设只读验收用
`verify --round-id` 加 `--contract`。只有原入口已配置通知且返回 `await_callback`
时，才告知用户等回传。

每次任务调用会重读边界配置，检查登记、目录授权和执行绑定；撤权后新的 CLI 调用不再
访问该项目，不中断已经运行的操作。原有记录保留，仍由有权操作者通过原入口核对和
收尾。通用入口不替代旧通知、接管和人工接受接口，项目也不会因关闭或核验通过而自动
视为用户接受。通用回调和真实 Bot 交付另行验证。

## 对话登记：Hermes Desktop 确认卡

同一个本机用户的已绑定 Desktop Bot Chat 可选装 `hab-project-registration` 插件。
复制安装包内 `herdr_agent_bridge.resources.hermes_project_registration` 的 `plugin.yaml`
和 `__init__.py` 到目标 Hermes profile 的 `plugins/hab-project-registration/`，
不要安装在共享默认 profile。用以下命令可以定位安装包内的目录：

```sh
python -c 'from importlib.resources import files; print(files("herdr_agent_bridge.resources.hermes_project_registration"))'
```

由操作者在插件目录写私有 `0600` 的 `settings.json`：

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

`session_id`、`chat_id`、`user_id` 取自 Hermes 已绑定 Bot Chat 的会话元数据，不用显示名称
或任务文本猜测。绑定的 owner 必须与 profile 和 `session_id` 一致；`bridge_python` 使用安装
了 bridge 的环境（保留虚拟环境的入口，不解析到裸 Python）。只在目标 profile 执行
`hermes -p NAMED_PROFILE plugins enable hab-project-registration`。无需注册模型工具。

Bot 找到候选后显示路径，请**用户在原对话**输入 `/hab-register 名称或绝对路径` 并在
Hermes 确认卡选择 **Register this directory**。卡片显示确切目录；普通文字「我同意」或
后台通知不能替代卡片确认。重名先选完整路径；已登记且仍获授权的目录直接返回原 ID。
取消或逾期不登记，提交前重读授权及目录身份；目录被替换时必须重新确认。登记只创建
目录身份，不写执行绑定、不派单。结果不明时查原目录，不从模型侧重放。

信任边界是已由 Hermes 认证的本机 viewer 和操作者保管的配置；这不是操作系统沙箱。
不要让不受信模型访问同一账号的原始 Hermes 控制 socket、插件配置、登记表或操作者
登记命令，或给它可绕过固定入口的任意本机执行权。首版不支持其他 Hermes 消息平台
或 Grok 的对话确认。保持 Hermes 的确认卡超时为有界值。

## 本机通知：Hermes Desktop Bot Relay

bridge 和 Hermes 同机、同一系统用户时，可在**创建新项目执行绑定**时通过
`init --notification-config /private/hermes-relay.json` 选用 Relay。旧 Grok HTTPS
配置及 `--webhook-config` 用法不变。Relay 不启动网关、不开放 HTTP 端口，也不需要
复制 Hermes 凭据。由操作者管理私有文件，建议 `0600`：

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

使用已运行的命名 profile 及**已绑定的本机 Desktop Bot Chat**；不要使用标题、最近会话
或 HTTP API 创建的未绑定会话。配置中的两个 ID 应与项目 `init` 的 owner 相同。用于
通用入口时，`init --project-id` 须取登记返回的 ID。新 run 会固定原 profile、对话、
项目与请求；旧 run 不迁移。Hermes 运行时应与其安装匹配，升级后重验 Relay 接口。

原生 `submit` 启动的观察器在有结果、问题或故障时将事件写入持久 outbox，并用
同一事件标识投递。通知只含事件、项目、请求、run、轮次和类型，不带提示正文、源码或
结果。它不会给 Bot 新增权限：发起任务的原用户轮可预先授权该任务的读取与命名核验，
Bot 仍须匹配原请求和轮次。后台事件不能确认登记、派单或代答权限。

`notify EVENT --request-id REQUEST` 对已接受或投递未知的 Relay 记录只查询原收据，不重发。
`relay.status=queued/claimed` 表示 Hermes 已收录，还未证明实际回复；`settled` 表示 Hermes
已完成该轮，也不等于任务核验或用户接受。`rejected/not_sent`（例如原 Bot Chat 已改变）才可
由操作者修复原路由后显式 `--retry`；`unknown` 先核对原收据，不能自动重投或改投其他会话。
`ack` 仍用于旧 Grok 路径，Relay 不用人工填回执替代 Hermes 的实际状态。

## 本机部署：让多个绑定共用项目占位

同一个规范化项目根目录只用一个共享启动槽位。项目别名、Bot 和对话可以不同；
共享名额不授予跨绑定读取或继续任务的权限。未启用共享占位的固定入口保留原用法。

本机操作者通过 Python API 启用。先使所有派单入口运行支持共享占位的版本，并一次性
传入该项目**全部现存**配置（特别是活动或投递未知的任务使用的配置）：

```python
from herdr_agent_bridge.project_bridge import Project

Project.enable_shared_starts("/private/old-project.json", "/private/another-existing-entry.json")
```

初始化只在 `.artifacts/project-admission/slot.json` 保存项目槽位和当前请求的指针；
没有调用方清单。找出多个未关闭或未知的历史任务时，必须先核对并收尾，再启用。
原配置、owner、run、journal 和 outbox 保留原位。后续启动只查同一个槽位，不扫描其他
入口的配置；重复调用初始化不会替换当前槽位。不要删除槽位文件或用旧版本绕过门控。
低层 `agent` / `manager` 调试接口不参与项目占位，不作为日常派单的替代入口。

同一项目的两个入口同时调用 `start` 时，只有一个可以在项目锁内登记启动意图并创建
run。拒绝其他请求时返回 `project_start_conflict`（已有活动 run）或
`project_admission_unresolved`（无法核对占用），均为 `delivery=not_attempted`，
也不会暴露其他对话的任务身份。已有任务仍由原入口核对；只有确认原 run 和专用资源
均已关闭，才能开启新任务。正常关闭时会在 run 目录写入小型 `run-closed.json` 凭证，
槽位优先核对该凭证，不必读取完整 journal。没有凭证的历史 run 仍用原关闭记录核对，
这些运行记录不受配置文件的 4 MiB 读取上限约束。

### 启动中断，又找不到 run

先用原入口和原 `REQUEST_ID` 核对；有 run 或创建证据时不能重发。
本机操作者可在**原配置**上执行：

```sh
herdr-bridge --config /private/original-project.json abandon-start REQUEST_ID
```

只有在请求还未绑定 run、并能证明没有启动时才可撤销。启用共享占位后，目标 run 目录
不存在或确实为空，即为一种证据：Manager 在接触 `hc` 前会先写入并同步创建记录。
另一个可恢复情形是，新请求只有初始 Manager 记录、尚未创建 Bridge，而同一入口原任务的
开发占位仍活动（原 run 已关闭）。此时先对**新请求**执行 `abandon-start` 留下撤销记录，
再用明确的操作者决定终止**旧任务**：

```sh
herdr-bridge --config /private/original-project.json decide terminate \
  --request-id ORIGINAL_REQUEST --actor OPERATOR --note 'original task ended'
```

两步都不启动 Agent，也不删除记录；下次派单使用新的请求 ID。`abandon-start` 不在 Bot
固定入口提供。已有 Bridge、已绑定 run，或没有上述证据的历史未知记录仍须从原入口核对。

## 启动、同任务继续与结束

既有项目默认使用 Pi，模型是 `openai/gpt-6.1-sol`，推理强度是 `high`。`config`
用 `agent` 和 `defaults` 显示当前项目策略，同时隐藏项目路径、Bot／对话绑定和通知配置地址。

只读调查使用 `start REQUEST_ID`。开发任务使用
`start REQUEST_ID --development-contract /private/path/task.json`，例如：

```json
{
  "task_id": "repair-report",
  "goal": "修复报告中已确认的计算错误",
  "allowed_paths": ["src/report.py", "tests/test_report.py"],
  "acceptance": ["既有合成数据覆盖修复结果，原有用例继续通过"],
  "checks": ["unit"]
}
```

开发 contract 中的 `checks` 只能引用项目已配置的检查名；未知、重复、越界或
符号链接路径会拒绝。`start` 只创建专用执行会话，不自动提交业务 prompt。
任务范围不明确时先做只读调查，再建立开发 contract。

开发任务文本默认使用 `ensure_ascii=False` 序列化可写文件、验收条件
和检查列表，直接显示中文，同时保留合法 JSON 的引号、反斜杠等转义。该默认适用于
后续新提交的轮次；已提交文本和终端历史保持原样。

所有命令都使用相同的配置前缀：

| 命令 | 用途 |
|---|---|
| `status --observe --wait 10` | 有界观察当前执行与关联结果；等待结束不等于任务成功 |
| `submit ROUND_ID --prompt-file /private/path/prompt.txt --monitor` | 向原 run 提交一轮输入，并启动有界观察 |
| `submit NEXT_ROUND --parent PREVIOUS_ROUND --prompt-file /private/path/followup.txt --monitor` | 原任务内澄清或修正；保留原轮次关联 |
| `native-command OPERATION_ID COMMAND --request-id REQUEST_ID [--arguments TEXT]` | 向原 run 发送并核对显式原生命令，操作 ID 与业务轮次 ID 各自独立 |
| `result` | 读取最新已保存状态和证据，不重新执行任务 |
| `interrupt ROUND_ID --actor OPERATOR --note 'change direction'` | 打断工作中的当前轮次，保留原会话；观察确认为 `interrupted` 后可用新轮次及原 `--parent` 补充要求 |
| `recovery-status` | 核对进程变化，返回当前代码、原生会话与终端的审查指纹 |
| `recover --fingerprint VALUE --actor OPERATOR --review TEXT` | 审查后在恢复的 shell 中启动绑定的原生会话，保留原任务上下文 |
| `verify` | 开发任务独立收集范围 diff、约定检查及最终代码指纹 |
| `decide accept --actor OPERATOR --note 'reviewed evidence'` | 在核验通过且代码未改变时记录技术接受 |
| `decide reject --actor OPERATOR --note 'needs correction'` | 保留原任务并记录需要继续修正 |
| `decide terminate --actor OPERATOR --note 'task stopped'` | 明确终止开发任务；run 已关闭也可记录终止，不会重开运行 |
| `close` | 清理该 run 的专用资源，保留项目成果和证据；不代替接受或终止开发任务 |

原生 Agent 确认实际模型、推理强度和 session ID 后，入口才开放输入和 `run_ready`
通知。`model_selection.requested` 是启动要求，`actual` 是已确认的值，
`state: pending` 表示仍在等待，`state: failed` 表示原生选择核对失败。后续轮次依靠
已绑定会话的原生证据。`execution_policy` 保存每个字段的请求值、解析值和来源。

Pi 要求原生 assistant 消息中的 provider/model 与启动选择一致；将虚拟模型
路由到不同实际 provider/model 的配置会返回 `native_dispatch_mismatch`，暂不支持。

`--monitor` 若返回 `monitor_error`，先核对观察进程，不能因此重发 `submit`。
原生 run 配置了通知时，`submit` 默认启动持久 hc 观察终端，不依赖 Bot 的 Shell
调用进程继续存活。`next_action=await_callback` 表示 Bot 应结束当前等待，由本机
观察器等待 hc 状态并回调；不要循环查询 status。`monitor_error` 时保留已投递的
任务，恢复观察器而不是重派任务。回调到达后只做一次身份校验及最新状态核对，
再获取结果、核验并回传。手动状态查询与异常恢复仍可使用现有入口。

原生任务的观察器使用 `hc events --terminal ADDRESS`，`monitor.observation=events`。
初始快照、状态变化、丢失事件后的快照及重连都会唤醒一次原任务核对。每条连接
按序号过滤重复事件，重连采用新快照建立当前状态；通知继续使用持久 outbox
去重。`monitor.subscription` 保存连接数、重连数、事件数及重复数等摘要。
终端已结束工作而原生证据仍在写入时，观察器进行有界补查。
也可用 `monitor --seconds 600` 独立启动观察，最长 3600 秒。未确认结束的观察
进程不能重复启动。任务提交回执、终端观察、关联结果、核验通过和操作者接受
分别记录；终端 `done` 或检查通过不代替用户实际体验和最终接受。

可给 run 命令加 `--request-id REQUEST_ID` 读取旧绑定；`status`、`result` 支持
`--task-id ROUND_ID`。只读 `verify` 需同时传 `--task-id ROUND_ID --contract NAME`，
仅适用于已有只读验收题，不用于验证任意产品需求。

开发任务的 App 接管继续使用 `takeover --actor OPERATOR`、`reconcile` 和
`resume --fingerprint VALUE --review TEXT`。接管期间自动输入停止；恢复前须核对
原会话和当前代码，不代答权限或 trust 弹窗。详见[开发任务接口](DEVELOPMENT.zh-CN.md)。

`interrupt` 在发送停止指令前保存操作者、原因及原轮次。每轮只发送一次停止指令，
重复调用返回原记录。回执未知时，通过 `status --observe` 核对原生中断记录及终端
状态；确认后该轮显示 `interrupted`，其结果仍保持未验收。当前原生 Pi 路径同时
核对 `agent_settled`，确认停止后再向同一会话提交补充要求。

新建原生终端启用 hc `keep`。进程丢失后 core 会恢复同地址的 shell；桥接层
检测进程变化，将 `recovery.state` 置为 `needed` 并暂停任务输入。先查看
`recovery-status` 返回的文件摘要和指纹，再执行 `recover`。恢复命令只启动指定
原生 Pi 会话文件或 Codex 线程。新进程中的模型、推理强度和会话身份匹配后显示
`restored`；原轮次保留为已中断，后续要求通过新轮次及 `--parent` 继续。
恢复回执未知时观察原 run，重复 `recover` 返回原恢复记录。恢复 shell 支持
sh、bash、zsh 或 dash。

## 核验、接管和关闭

开发核验先保存原轮次、代码和原生会话的指纹，再释放状态锁运行命名检查。
此时 `status` 仍可查询，`development.verification_job` 显示核验状态；
`input_ready=false`，新的输入和重复核验明确拒绝，不会等待检查结束后悄悄派发。

`takeover` 会暂停自动输入并取消运行中的核验。检查进程组停止并回收后，核验返回
`passed=false`、`reason=verification_cancelled`；接管前的结果不能用于接受任务。
`reject`、`terminate` 或原生会话/代码变化也会使本轮核验失效。

`close` 会先请求取消核验。如果独立核验 lease 仍被检查持有，返回 `closed=false`
和未通过的清理项；这不代表清理完成。先查询原 run，确认核验结束，再重试该 run
的 `close`。核验父进程意外退出时，检查子进程继承的 lease 仍阻止重跑和过早清理。
确认 lease 已释放后，显式 `reconcile` 可将遗留核验标记为 `verification_interrupted`；
它不自动重新运行检查或提交任务。

命名检查必须是有界命令。主检查退出却留下进程组成员时会终止该组，并报告
`check_left_descendants`，不能标记为通过；预览服务应独立管理生命周期。
这不是恶意进程沙箱，不保证捕获主动脱离进程组的程序。

Pi 证据的等待/失败原因通过 `model_selection.reason` 和轮次 `diagnostic` 返回，
例如 `native_session_missing`、`native_trailing_write`、`native_version_unsupported`、
`native_history_ambiguous`。原因码不包含原始记录或私有路径；缺失或部分写入可继续
观察，明确不支持的记录应先核对。不要因此重派任务。

项目 CLI 的失败 JSON 保留原有 `error`、`delivery` 和恢复提示，并增加 `code`
与 `operation`。例如无效模型策略返回 `execution_policy_invalid`。这些字段用于
定位失败阶段，不能据此把 `delivery=unknown` 改判为未投递。

## 默认值与单次覆盖

`start REQUEST_ID --agent pi|codex --model MODEL --thinking LEVEL` 只覆盖该次启动。
`--agent` 可省略，沿用项目默认。Pi 的 `MODEL` 采用 `PROVIDER/MODEL`，Codex 使用
原生模型 ID，例如 `gpt-6.2-sol`。`--reasoning` 与 `--thinking` 等效。
Codex 开发任务需要 `native` runtime。

新配置可以用 `init --agent codex` 选择 Codex 作为默认项。`defaults --agent codex`
切换项目默认，`defaults --model MODEL --thinking LEVEL` 可以一同设置模型和推理强度。
同 Agent 下未指定的字段继承项目当前值。切换项目默认 Agent，或启动时显式选择另一
Agent 时，未指定的字段使用所选 Agent 的内置值（Pi 为 `openai/gpt-6.1-sol/high`，
Codex 为 `gpt-6.1-terra/max`）。`execution_policy.sources` 将字段来源标为
`task_override`、`project_default` 或 `agent_default`。

两种 Agent 各自校验模型格式和推理强度。能在启动前识别的错误会返回
`execution_policy_invalid` 和 `delivery: not_attempted`，项目名额仍可使用。实际模型
和推理强度以原生证据为准。项目默认仅适用于新请求，修改项目默认也无需修改全局 Agent 配置。

## 原生命令

用户明确要求对当前会话执行原生操作时，使用稳定的操作 ID。固定入口例如：

```sh
herdr-bridge --config /private/project.json native-command mode-fast /fast --request-id REQUEST_ID
herdr-bridge --config /private/project.json native-command goal-one /goal --request-id REQUEST_ID --arguments 'Reduce report generation time'
```

Personal Agent 包装器同样支持 `native-command`，并要求同时填写 `--project-id` 和
`--request-id`。Pi 会话可以用 `native-command OPERATION_ID /name --arguments TITLE`
设置会话名称。业务输入继续使用 `submit`，正文提及 `/fast` 时仍按普通文字投递。
启用 Codex Fast 后查原命令的 `native_operation.state`，只有确认生效后才提交需要 Fast
的开发任务。若原生命令需要选择、仍在执行或结果未知，用原请求和操作 ID 核对。`/goal`
可能启动自己的模型轮次，等它有明确结果后再提交下一轮业务输入。

## 重启和投递未知

每次启动需要稳定的 `REQUEST_ID`。相同 ID、相同启动参数再次调用 `start` 只
恢复原 run；改选 Agent、模型或推理强度会返回 `project_start_mismatch`。重试时省略
`--agent` 会沿用原请求的 Agent。重试仍传原请求的模型和推理强度覆盖项，原本省略的
字段继续使用已保存的解析结果。旧记录缺少 Agent 字段时按当时固定的 Pi 入口读取。
前一 run 未关闭时不能创建第二个活动 run；关闭后才能以新
请求 ID 建立下一任务。已关闭请求的旧 ID 不会重启。

项目会在任何 Manager／hc 副作用前持久保存启动意图。如果创建中断、超时或
回执丢失，先通过配置找回原绑定，使用 `status`／`status --observe` 核对原任务。
如果意图已保存但 `manager.json` 尚未建立，入口返回 `creation unresolved` 并
保留原指针；不能删除绑定、换 ID 或重复创建来绕过不确定状态。此情况需要
检查专用目录及实际会话，确认副作用后再由操作者处理恢复。

输入投递未知沿用原任务门控，禁止盲目重发。失败 JSON 中的 `delivery: unknown`
是保守的恢复提示，并不证明输入未送达。CLI 不返回原始异常、命令或 traceback。

## 通知处理与私有边界

routine 收到通知后将载荷保存到每次唯一的私有文件，再执行
`notification --payload-file /private/path/unique-event.json`。入口把通知视为不可信指针：
只处理当前项目已绑定的 run：新目录使用 `.artifacts/runs/RUN_NAME`，历史目录
按[安装说明](INSTALL.zh-CN.md#runtime-and-acceptance-directories)原位兼容。两类指针均检查 owner、
run ID、事件 ID 及已保存事件身份。不能用通知提供的任意路径读取其他文件，也
不能根据通知新建任务或自动重放 prompt。

后续 `status`、`verify`、`result`、`ack` 必须显式使用返回的
`--request-id REQUEST_ID`，不能用当前活动请求代替迟到事件的原请求。
活跃 run 先刷新状态再返回结果；关闭 run 只读取保存状态，不再次观察或提交。
需要记录接收时，使用 `notification ... --receipt RECEIPT`；或
`ack EVENT_ID --receipt RECEIPT` 走相同校验路径。`notify EVENT_ID` 只投递已有
outbox 事件；`--retry` 仅适用于明确拒绝的通知，不重试未知投递。
HTTP 接受、routine 回执和任务成功是不同事实；组合 receipt 不证明平台原生
消息 ID 或独立 UI 落点。

CLI 的 checks、contract 和 callback JSON 文件限 64 KiB；重复键、非有限数值、
非法编码和过深嵌套会安全拒绝。项目配置限 4 MiB，必须是非符号链接的 `0600`
普通文件。原 run 路径及关键状态文件的符号链接会拒绝；项目和 Manager 的
webhook 引用不同也会拒绝，须先显式核对绑定，不静默改向另一个 endpoint。

公开结果过滤已知私有路径、Bot／对话身份及已加载 webhook 密钥／地址，包括
动态对象键。固定协议字段保留原名；若密钥本身出现在固定字段名中，绑定失败，
避免隐藏校验字段或输出密钥。请求参数也检查当前加载的 webhook 密钥。

授权范围内的代码 diff、结果和检查输出仍会返回指定 Grok 对话。这不是通用
秘密扫描器；任意项目内容、检查 stdout 或环境中的未知秘密无法据此保证被
过滤。只接入可信项目和已检查的命名命令，不允许敏感数据进入授权文件或输出。
专用会话和工具文件范围不是同账号进程之间的安全隔离或 OS 沙箱。

## 本地验证

```sh
uv run --locked --python 3.11 python -m unittest discover -s tests -p test_project_bridge.py -v
uv run --locked --python 3.11 python -m unittest discover -s tests -v
```

项目入口测试使用临时 Git 项目、独立 Pi 配置和 fake hc（含子进程 CLI fixture），
覆盖默认与按字段覆盖、持久恢复、禁止重复派单、未知创建／投递、同任务继续、
清理、通知身份／路径检查、配置漂移及私有输出。它们不调用模型或真实网络，
不证明真实 Pi、Grok 或 Herdr App 已接通。
