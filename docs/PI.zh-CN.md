# Pi 接入

[English](PI.md) | 中文

Pi 沿用 `Grok Bot → 管理 CLI → hc` 链路。日常原生开发见[固定项目入口](PROJECT.zh-CN.md)，
开发 contract 和接管接口见[开发任务接口](DEVELOPMENT.zh-CN.md)。
本页说明底层 `isolated` 专用会话、只读工具和认证策略。
底层 manager 的默认 Agent 是 Codex，既有未记录 `agent` 字段的 manager journal 继续按 Codex 处理；固定项目入口的默认值另见项目指南。

## 本机预检与启动

需要 Python 3.11+、Node.js、已安装并配置模型的 Pi 和 hc。
当前针对 Pi 1.5.0、hc 0.1.18、Linux 验证；其他版本应重新真实验收。

```sh
herdr-bridge manager probe --agent pi
uv run --locked --python 3.11 python -m unittest discover -s tests -v
```

预检在临时 HOME/XDG/Pi 配置目录中只查询 hc 和 Pi 版本，不要求安装 Codex，
不读取认证、不启动 Agent，也不证明模型或 Grok 可用。

通过已登记的本机入口，在仓库根目录启动一个新的专用 run：

```sh
herdr-bridge manager --run .artifacts/runs/<unique-pi-run> \
  --bot-id <bound-bot-id> --conversation-id <bound-conversation-id> \
  start --agent pi --project "$PWD" --webhook-config <private-config-file>
```

通知配置可暂时省略。新 run 使用 `.artifacts/runs/`，与 Agent 类型和调用方无关。

Pi 从 `PI_CODING_AGENT_DIR`，或默认 `~/.pi/agent`，读取已保存的
`defaultProvider`、`defaultModel` 和思考级别。可用
`--model <provider>/<model-id>` 明确覆盖；已保存的 model ID 可以包含 `/`。
未配置模型时明确报错，不套用 Codex 默认模型。

返回 `agent=pi` 后，使用相同 run、Bot、对话及 `--expect-run-id` 继续
`observe`、`submit`、`verify`、`ack` 和 `close`。Agent 类型固定在 run 内，
后续调用不再次选择，不能通过重启命令切换既有 run。
等待 `input_ready=true` 再输入；完整命令及通知恢复流程沿用
[管理契约](GROK.zh-CN.md)。

```sh
herdr-bridge manager --run .artifacts/runs/<unique-pi-run> \
  --bot-id <bound-bot-id> --conversation-id <bound-conversation-id> \
  --expect-run-id <returned-run-id> \
  submit analysis-pi --contract analysis --monitor
```

自定义任务仍用 `--prompt-file`，需要另外定义独立验收，不能套用无关的内置验收题。
底层本地入口也支持 `herdr-bridge agent --run <private-run> start --agent pi --project <project>`。

## 配置、认证与执行边界

- hc 使用随机专用 session 和临时 XDG 路径；Pi 使用独立配置、模型缓存和会话目录。
  只带入模型选择及该 provider 的模型配置，不复制用户的扩展、包、MCP、trust、
  全局指令、历史会话或界面设置。项目和祖先目录的上下文文件仍按 Pi 行为加载。
- 保留 `httpProxy`、`httpIdleTimeoutMs`、`websocketConnectTimeoutMs` 和 `transport`
  网络设置。SDK 在创建 ModelRuntime 前使用 Pi 安装依赖的 undici 初始化
  dispatcher／fetch；已有环境代理优先，兼容 `NO_PROXY`，不输出代理地址。
  关闭时释放本进程 dispatcher。代理配置可能含认证，仍只保存在私有临时配置。
- 只开放 `read,grep,find,ls`。关闭扩展、MCP、skills、prompt templates 和 themes
  发现；拒绝加载需要信任的项目配置。关闭自动压缩和 Agent 自动重试。
  API-key 路径使用官方交互 CLI；OAuth 路径使用官方 SDK 的 `InteractiveMode`。
- API-key 认证仅复制选定 provider 到权限 600 的临时文件，关闭时移除。
  OAuth **不复制令牌**：SDK 的 `ModelRuntime` 显式使用原认证文件的 `authPath`，
  刷新和持久化由 Pi 原生文件锁负责。原认证文件按日常 Pi 使用方式保留和更新，
  清理只删除临时指针及本 run 资源，不删除原认证。
- OAuth 路径共享整个原生认证存储，不是按 provider 隔离的凭据库。Pi 可能为
  availability 检查读取其他 provider 的认证；实际任务固定使用选定模型。
  启动前检查选定模型配置及共享存储中的 API-key 配置，拒绝 `!command` 形式。
  检查是当时的快照，不是对其他进程修改认证文件的持续约束。
- 过滤继承的 hc/Pi 会话标记和 Node 预加载配置。保留运行模型所需的普通环境，
  如 PATH、代理、证书及 provider 环境变量。
- 只读工具选择、专用状态和拒绝项目 trust 都不是操作系统沙箱。Pi 进程仍有
  启动账号的权限，读取工具也没有项目目录的硬边界；不将它记作 Codex 的
  `read-only/on-request` 沙箱。工具不会代答权限或信任提示。

## 结果证据

`pi_evidence.py` 只读取本 run 独立目录中的单一 Pi v3 JSONL 会话。
精确匹配提交的完整 user 文本，保留原生 session ID 和 user message entry ID。
Pi 没有 Codex 风格的 turn ID，因此 `agent_turn_id` 使用真实 user entry ID，
私有证据标记 `turn_id_source=pi_user_message_id`。

通常只有该 user 后、下一个 user 前的最后一条有效 assistant 文本，且
`stopReason=stop`、无待执行 tool call，才算原生响应完成。仅针对 **native Pi
开发任务结果**，可将严格识别的 Herdr two-pack 中 Coder 和 Cleaner 回执通知视为
同一原始轮次的延续：线性历史必须在通知前已有 pack／worker 绑定，并由匹配且
非错误的工具调用及已识别的 two-pack 快照命令结构化结果另外佐证已接受的
回执；其他通知或快照格式仍不关联。最终结果必须属于原任务。独立输入、无法
确认或不匹配的回执、问题和已有明确边界的最终答复均不适用。
轮次 ID 仍是原 user entry，最终消息 ID 仍是实际 assistant entry。isolated Pi
和普通直接轮次保持原行为。

这只是核对会话记录的一致性，**不提供发送者的密码学身份认证**：能控制原生
历史或工具输出的参与者可以伪造这些观察。关联时不会执行通知中的命令，也不会
打开其中提到的文件。支持的记录格式不证明任务成功；变更或未知格式仍不关联。
此外还须观察到 hc turn 增长及 idle/done，native Pi 的 settled 记录也须指向
同一最终消息。问题、任务结果和独立核验成功仍分别记录；CLI 成功或终端 done
不算任务成功。

多会话、重复匹配、分支、fork、context edit、compaction、未知格式和中段损坏
不会退回屏幕正则猜测。末尾未写完的记录不能完成当前任务；应观察原任务，
不能重新派发。这里只支持短程线性会话，升级 Pi 后需重新验证格式及 SDK 行为。

## 可重复的真实本地验收

```sh
herdr-bridge tool accept_pi
```

该命令会调用已配置模型，创建专用资源，完成分析、同会话追问、确定性语言问题
与脚本预设 English 答复继续。它通过管理层做源码／引用核验；一次真实 send
返回后在本地丢弃回执，再核对原任务且拒绝重发。这不是实际断网或实际人工答复。

结果、原生会话快照和调用记录保存在
`.artifacts/acceptance/pi/<acceptance-id>/`；报告中的 `run` 指向
`.artifacts/runs/<run-id>/` 中的 Manager 和 Bridge 状态。失败时也尝试保存本 run 原生证据，再清理专用进程与
临时认证；快照可能包含未写完的一条记录。原始会话可能含项目内容，只在私有
`.artifacts/` 留存。OAuth 刷新需要原认证目录可写；外层拒绝时保留失败记录，
不绕过授权、不登录新账号、不盲目重发任务。

本地验收与真实 Grok 派单／通知落点分开记录。不能从本地协调器或 fixture
通过推定 Grok → Pi 闭环已经验收。

## 来源

- [Pi CLI 与工具选项](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)
- [Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)
- [认证 SDK 示例](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/examples/sdk/09-api-keys-and-oauth.ts)
- [会话格式](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)
- [Pi 安全与项目 trust](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)
