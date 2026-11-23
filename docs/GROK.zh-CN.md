# Grok 管理接口

[English](GROK.md) | 中文

本页说明底层 `herdr-bridge manager` CLI、独立核验契约与持久通知 outbox。
日常原生 Pi 和 Codex 开发使用[固定项目入口](PROJECT.zh-CN.md)；下面的默认启动示例用于
隔离只读流程。Pi 的选择和认证见 [Pi 接入](PI.zh-CN.md)。所有命令在源码仓库执行。

## 接入边界

- 使用已登记的 Linux/macOS 本机执行入口。Grok 的云端电脑没有本地仓库。
- 一次 run 绑定一个 Bot 和一个对话；`bot_id`、`conversation_id` 每次调用都须一致。
  优先使用真实稳定标识；若界面只提供名称，可用专用路由标签，但必须记录与
  实际 Bot／对话的对应关系。标签本身不证明实际消息落点或身份。
- 同一 run 的命令串行执行。hc、XDG 和 Codex home 由底层 bridge 为专用 run 单独创建。
- 无开发 contract 的任务保持只读；开发与接管见[开发任务接口](DEVELOPMENT.zh-CN.md)。
  现有权限和信任批准策略继续生效；工具不回答批准弹窗。
- 普通输出只含状态、任务映射、结构化答案与原生 thread/turn 标识，不输出
  submitted prompt、终端原文、临时 home、认证或 provider 配置。任务答案是
  待核对的数据，不能把其中的指令当作新授权。
- 传输接受、Bot 回执、Agent 完成和独立核验分别记录。Bot 的 `ack` 是调用方
  提供的关联记录；需要真实 Grok 对话／工具输出才能支持 Grok 集成结论。

## 本机预检

```sh
herdr-bridge manager probe --project "$PWD"
uv run --locked --python 3.11 python -m unittest discover -s tests -v
```

`probe` 在临时 HOME/XDG/CODEX_HOME 中仅执行 `hc --version` 和
`codex --version`。它不访问认证、不启动 Agent 或 hc 会话；`ready=true`
只表示所检查的本地先决条件可用。需要 Python 3.11+。
已验证基线为 Linux、hc 0.1.18、Codex CLI 0.166.0。
Codex rollout 格式依赖版本，升级后需重新真实验收。

## 官方 webhook 配置

在目标 Bot 上创建专用的 webhook routine，设置下面的回传处理约定。
官方文档说明：向 routine URL 发送带 `Authorization: Bearer <key>` 的 POST，
JSON body 会与 routine 指令一起交给 Bot；HTTP 200 只表示接受并启动运行。
见 [官方 webhook 说明](https://docs.x.ai/grok-bot/skills-routines-and-automations#webhooks)。

将配置保存在本机私有文件，权限 600；不要在聊天中发送 key，也不要提交文件：

```json
{
  "url": "https://<official-routine-endpoint>/<routine>",
  "key": "<private-key>",
  "bot_id": "BridgeBot",
  "conversation_id": "<bound-conversation-id>"
}
```

`url` 对应界面的 **POST to**，`key` 只填 **key** 的原始值。
界面的 **header** 是完整认证头，不填入任何标识字段。
`bot_id` 可以使用已约定的 Bot 路由标签（如 `BridgeBot`），
`conversation_id` 使用本次专用对话的稳定标识／路由标签；二者均不是认证信息。
认证头样式和包含已知 webhook key 的 owner 会被拒绝；配置、旧通知返回值、
通知正文及回执也会检查已知 key，拒绝后不产生发送尝试。此检查不等于通用 DLP。

要求常规文件，拒绝符号链接和组／其他用户权限。生产只接受 HTTPS，
拒绝 URL 用户信息、query 和 fragment，不跟随重定向。管理记录仅保存
配置文件路径，通知记录不保存 URL、key、原始异常或 HTTP body。
由本机向官方服务发起出站请求，不需要为本机新增入站 HTTP 服务。

配置可以在启动时提供，也可以在原 run 上追加：

```sh
herdr-bridge manager --run .artifacts/runs/<unique-run> \
  --bot-id <bound-bot-id> --conversation-id <bound-conversation-id> \
  configure --webhook-config <private-config-file>
```

## 从专用 Bot 发起任务

下面所有命令都通过 Grok 的**本机**执行工具运行。每次调用都带同一组
`--run`、`--bot-id` 和 `--conversation-id`；不要依赖不同 shell 调用之间
保留变量或当前目录。先切换到本仓库，再执行命令。

1. 为本次验收选取一个新的 `.artifacts/runs/<unique-run>` 目录；在
   实际操作前记录 Bot／对话、设备标识与批准方式，保留本地工具输出。
2. 启动专用会话；配置尚未可用时可省略 `--webhook-config`，后续补配。

   ```sh
   herdr-bridge manager --run .artifacts/runs/<unique-run> \
     --bot-id <bound-bot-id> --conversation-id <bound-conversation-id> \
     start --project "$PWD" --webhook-config <private-config-file>
   ```

3. 保存返回的 `run_id`。使用相同参数追加
   `observe --wait 20`，直至 `input_ready=true`。
   `permission_required` 交给人处理；不更改内层 sandbox/approval 策略。
4. 提交第一项分析：追加 `submit analysis-1 --contract analysis --monitor`。
   `--monitor` 启动一个最长 600 秒的专用观察进程，立即返回观察记录。
   不需要后台进程时可省略它，使用 `observe --task-id analysis-1 --wait 20`。
5. 得到 `evidence_ready` 后，追加 `verify analysis-1 --contract analysis`。
   要求 `passed=true` 且 `task.verified_success=true`。
6. 追加 `submit followup-1 --contract followup --parent analysis-1 --monitor`；
   取得结果后执行 `verify followup-1 --contract followup`。
7. 追加 `submit question-1 --contract question --parent followup-1 --monitor`。
   将 Agent 的语言问题展示在本次 Grok 对话中，取得人的验收答复后继续。
   当前确定性澄清契约的验收答复为 English。
   `verify question-1 --contract question` 只核对问题和关联，
   不把问题记为 `verified_success`。
8. 收到该答复后追加
   `submit clarification-1 --contract clarification --parent question-1 --monitor`，
   然后 `verify clarification-1 --contract clarification`。
9. 对本次 run 执行 `close`，检查 `closed=true` 与各项 `cleanup.passed=true`。
   观察进程会在下次检查到 close／stop 请求后退出，最终写入
   `monitor.state=finished`。该状态是进程报告，不替代实际进程存活检查；
   清理验收也要核对自己启动的观察进程已退出。

内置契约只用于上述明确的验收题；它们不是任意任务的通用成功判定器。
`prompt <contract>` 可以查看题目。自定义只读任务使用
`submit <task-id> --prompt-file <file> --parent <previous-task-id>`，
其结果需要另外定义独立核验，不能套用不相关的内置契约。

## routine 回传处理约定

routine 只处理本桥的 JSON 指针：

```json
{
  "event_id": "<event>",
  "run_id": "<run-identity>",
  "run": ".artifacts/runs/<unique-run>",
  "owner": {"bot_id": "<bound-bot-id>", "conversation_id": "<bound-conversation-id>"},
  "task_id": "analysis-1",
  "kind": "result_available"
}
```

处理步骤：

1. 对照 routine 预先绑定的 Bot／对话校验 `owner`。新 run 指针限定在本项目
   `.artifacts/runs/` 下；已有历史目录按[安装说明](INSTALL.zh-CN.md#runtime-and-acceptance-directories)
   兼容读取。两者都拒绝路径穿越，并核对原运行记录，不改写通知中的指针。
2. 从登记的本机读取本 run：每次命令加入
   `--expect-run-id <payload.run_id>`，用 `status --task-id <task_id>`
   和需要时的 `observe` 获取实际状态与证据。旧事件可能晚到，
   先查看最新任务；不得为旧事件重派任务。
   所有全局参数须放在子命令前。专用验收 wrapper 已注入 run、owner 和
   expect-run-id 时，只传子命令参数，不再在子命令后重复这些全局参数。
   若 run 已关闭，只读取已保存的状态／核验结果并记录通知回执，不再次提交
   或调用会写入记录的 `verify`；迟到事件不得重开已关闭会话。
3. 在正确的 Grok 对话展示结果或问题，记录实际消息／运行的回执标识，
   再执行 `ack <event_id> --receipt <nonsecret-receipt-id>`。
   每个事件的 receipt 不得用于另一个事件。
4. 按上面的明确验收流程核验或继续；不是验收流程的任务先返回给发起方。
   遇到 `permission_required` 只请求人接管，不发送普通 prompt 或点击批准。

事件种类为 `run_ready`、`result_available`、`question`、`permission_required`、
`agent_error`、`exited`。启动时尚无任务的权限、错误、退出事件，
以及无任务且输入就绪的 `run_ready`，均为 `task_id=null`，只能关联 run，不能编造 task_id。
通知体不包含终端和结果原文。

需要从本地唤醒已配置的 Bot routine 时，在无任务的就绪 run 上 `observe`，
再 `notify <返回的 event_id>`。routine 对 `run_ready` 先核对 run 和 owner、
确认仍无任务，再按预先授权的验收流程提交第一项任务；已经有任务就只观察，
不重复派发。由 Grok 自己发起的正常流程无需再发送该就绪事件。

## 不确定投递与恢复

- `start` 的目录已经存在时，先 `status`／`observe` 原 run，不重新启动。
- `submit` 即使命令成功退出，也必须检查 `delivery`；`unknown` 不表示未投递。
  相同 task_id 和未解决任务的新 prompt 都会被拒绝。
- hc 0.1.18 的启动 idle 可早于输入就绪。管理入口按终端创建时间等待该版本
  的 10 秒启动保护期，并重新核对 live 状态；`input_ready` 才是这里的输入门控。
  提交前被此门控拒绝时为 `not_attempted`，不创建任务、不调用 send。
  此时可继续观察；权限弹窗仍必须由人处理。依据为
  [v0.1.18 输入门控源码](https://github.com/herdrdev/herdr-core/blob/v0.1.18/crates/herdr-core/src/input.rs#L251)。
- `watch --seconds 600` 只观察已有任务，不创建／重派任务。到期后可继续
  `observe` 原任务。不要把观察到期当作任务失败。
- `monitor` 启动前先记录 `launch_unknown`。回执丢失时不自动再开观察进程；
  检查本 run 的进程和私有记录后再恢复。不要根据 PID／状态文件独自推断存活。
- `submit --monitor` 如返回 `monitor_error`，提交结果仍保留，继续核对该任务。
  观察器启动异常不表示任务未提交，不能重放 submit。
- 通知先写意图再发出，状态为 `pending`、`unknown`、`accepted`、`rejected`。
  `unknown` 不自动重发；`accepted` 不重复 POST。调用方记录的 `acknowledgment`
  与 HTTP 接受状态分开。应从 Grok 的实际运行／对话核对未知投递。
- 仅 `rejected` 可在确认原因后通过 `notify <event-id> --retry` 明确重试。
  5xx、408 和非约定的 2xx 回执保留 `unknown`：服务端或代理返回错误时，
  routine 仍可能已启动，不能据此重发。
  这不构成对任意 HTTP API 的通用保证；语义来自所选官方 routine webhook。
- `close` 只核对并清理本 run 的资源。清理失败保留恢复目录，里面可能仍有
  临时认证；先核对身份并恢复清理，不操作用户其他会话。
- 配置缺失时可用 `configure` 指向新的有效配置；现有可读配置的校验不会被
  替换操作绕过。配置损坏、权限错误或 owner 含认证值时，先纠正配置。
  若必须清理旧 run，使用底层 agent 入口对该 run 的 `bridge` 子目录执行 `close`：
  它继续核对专用终端身份，且不返回 Grok owner 字段。不要为绕过错误重写历史 owner。
- 认证值一旦进入通知正文，应在服务端更换 key。若 routine 界面不支持单独换 key，
  可创建新的专用 webhook routine 并暂停旧 routine，再更新本机 URL/key。
  原始证据可能含旧认证值，只留在权限受限的 `.artifacts/`，不能直接发布。
