# 安全

[English](SECURITY.md) · [安装说明](docs/INSTALL.zh-CN.md)

## 私下报告漏洞

请登录 GitHub，使用 **M1racleShih/herdr-agent-bridge** 的[私有漏洞报告](https://github.com/M1racleShih/herdr-agent-bridge/security/advisories/new)，或在仓库的 Security → Advisories 页面选择“Report a vulnerability”。不要在公开 issue 或 pull request 中发布漏洞细节、可运行的利用代码、凭据或私有项目数据。如果私有报告不可用，请通过已有的私下渠道联系维护者；如果没有这样的渠道，可创建 issue 只询问私下安全联系方式，不描述漏洞。

请提供受影响的 bridge 版本和已知 commit、操作系统、hc 与相关 Agent 的版本、前提条件、使用合成数据的最小复现、预期与实际行为以及可能影响，并标明不确定之处。不要发送真实 token、原始对话、私有源码或完整运行 journal。维护者可通过私有报告协调修复与后续披露；本项目不承诺具体响应或披露时间。

当前 0.1.0 版本的报告在处理范围内。第三方漏洞也应按相应上游项目的安全流程报告。

## 运行边界

bridge 会运行本地程序，也能启动 coding agent；实际访问权限取决于其运行模式、项目和调用用户的权限。原生 Agent 配置和工具的权限可能大于隔离验收 fixture。授权执行前请检查任务、项目、Agent、模型和命名检查；命名检查会执行项目代码，路径限制不是 OS sandbox。权限和信任提示由人处理。

项目绑定、认证、webhook 凭据、journal、原生会话历史和通知状态应私下保存。工具的临时状态隔离不能防御以同一用户运行的其他进程。超时或丢失回复后保留并检查原记录；任务或通知送达未知时必须先核对，再决定是否重试。[项目指南](docs/PROJECT.zh-CN.md)说明操作和恢复，[贡献指南](CONTRIBUTING.zh-CN.md)区分模拟测试和真实验收。
