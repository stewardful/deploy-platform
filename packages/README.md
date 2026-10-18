# Go 与 TypeScript 可复用库

这里放被其他程序导入的代码包。它们与 [Rust crates](../crates/README.md) 都是库；与 [distribution](../distribution/README.md) 的安装/发布工具职责不同。

| 目录 | 使用方 | 能力 |
| --- | --- | --- |
| [go/agentapi](go/agentapi/README.md) | Go 控制端或其他 Go 程序 | Agent 类型、输入校验、Unix HTTP 客户端 |
| [ts/control-client](ts/control-client/README.md) | Web 或其他 TS 程序 | 控制端类型、请求、错误和校验；不依赖 React |

Go 模块由根 `go.work` 组织；TS 库由根 npm workspaces 组织。SDK 不拥有节点状态，不直接读数据库或执行容器命令，超时后不自动换请求键重投变更。

复用可先采用本地模块路径；对外发布前需确定仓库模块地址、版本和许可证。目录独立不等于已经发布到公共包仓库。
