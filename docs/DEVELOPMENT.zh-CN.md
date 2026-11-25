# 开发任务接口

[English](DEVELOPMENT.md) | 中文

日常原生 Pi 和 Codex 使用[固定项目入口](PROJECT.zh-CN.md)，保留正常 Agent 配置、工具和 App。
本页说明底层开发 contract 与接管接口；下面的 SDK 限定工具及私有 App 配置
用于 `isolated` 模式，不是日常原生模式的资源策略。


在原生固定项目入口运行 `start REQUEST_ID --agent codex --development-contract FILE`，
或选择 `--agent pi`。该契约固定任务目标、`task_id`、`allowed_paths`、`acceptance`
及项目中已配置的检查名组成的 `checks` 列表；入口将名称展开为登记的 `name`、
`argv` 和 `timeout` 定义。每个项目只允许一个未结束的开发 run；新 run 还要求前一个
已清理。无 contract 的入口保持只读。

在原生开发轮次中，是否使用子 Agent 或创建 Git 提交，取决于任务指令、项目规则和可用工具。
Bridge 跟踪主会话，并对照任务开始时的工作区基线核验改动。

固定项目入口用 `submit <round-id> --prompt-file <file>` 提交一轮，后续轮次用
`--parent <latest-round-id>` 关联。底层 `herdr-bridge manager` 接收包含完整检查定义
的契约，提交时使用 `submit <round-id> --development-task <task-id> --prompt-file <file>`；
它的 `submit ... --operator` 只准备并登记 App 中要输入的文本，绝不发送。这些
manager 专用参数不能用于固定项目命令。

`takeover --actor <operator>` 持久暂停自动输入。操作者实际输入准备好的文本后，
通过对应入口核对原生回答（固定入口用 `status --observe`，manager 用 `observe`）。
`reconcile` 取得当前文件／会话指纹，再用 `resume --fingerprint <sha> --review <note>` 恢复。

固定入口的 `verify` 和 manager 的 `verify-development` 独立取得基线 diff、检查命令及
退出码，验证检查期间源码未变、无范围外修改。`decide accept|reject|terminate --actor <operator> --note <note>` 单独记录
验收决定；接受要求最后核验仍覆盖当前代码及会话。检查通过本身不意味着人工接受。

Pi 的开发 SDK 仅新增指定文件的 `scoped_edit`、`scoped_write` 和预授权的
`run_check`；没有开放任意 bash。命名检查仍执行项目代码，并不是 OS 沙箱。
只读工具、共享原生 OAuth 的历史边界继续适用。

`herdr-bridge app --run <run> start [--display <private-display>]` 在同一私有 core 中
启动独立 App 窗口；随后 `bind` 显示执行 Agent，`status` 保存映射，`shot --output <png>`
截图。先 `close` 关闭本次 App，再清理 bridge。任何 launch/bind 回执不确定都应先
检查原记录，不能盲目重复。App 配置、会话、测试显示均与用户日常窗口分离。


## 新增 Agent 适配

在 [Agent 能力声明](../src/herdr_agent_bridge/agent_capabilities.py) 登记 Agent 的开发运行模式
和显式原生命令。固定入口与 Bridge 在占用开发名额前检查该声明。启动参数和模型选择由
[Agent 适配代码](../src/herdr_agent_bridge/agent_adapters.py) 提供。每条新命令还须在
[原生命令处理](../src/herdr_agent_bridge/native_commands.py) 实现发送前检查和原生状态回读。
验证同一操作 ID 重试、普通输入冲突及未知送达，使用
[共用命令契约测试](../tests/test_native_commands.py)。
