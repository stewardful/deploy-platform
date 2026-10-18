# 项目地图：先找入口，再找组件

这是一个包含 Rust、Go、TypeScript 的 monorepo。先按“运行的应用、可复用库、安装与分发”理解，不需要一次读完所有模块。

## 三类目录

| 类别 | 路径 | 如何判断新代码放哪里 |
| --- | --- | --- |
| 应用 | [apps](../apps/README.md) | 接收程序配置，组装组件，管理启动/退出，或提供具体页面 |
| Rust 库 | [crates](../crates/README.md) | 能被其他 Rust 程序引用的模型、接口、流程或适配器 |
| Go/TS 库 | [packages](../packages/README.md) | 能被其他 Go/TS 程序引用的客户端和协议类型 |
| 安装与分发 | [distribution](../distribution/README.md) | 生成安装包、部署系统服务、升级和节点验收 |

`packages` 与 `crates` 是同一层概念，只是语言习惯不同。`distribution` 原名 `packaging`，重命名是为了与代码包区分。没有把目录数量变成进程数量：Agent 的多个 crate 编译成一个服务；Web 构建后由 Go 托管。

## 一次网页部署经过哪里

```text
apps/web 的部署页面
  → packages/ts/control-client：调用控制 API
  → apps/controlplane/httpapi：认证与 HTTP 请求
  → apps/controlplane/control：管理编排
  → packages/go/agentapi：Agent Unix HTTP 客户端
  → crates/agent-api：私有 Socket 服务
  → crates/agent-core/src/service/：持久化任务、单 worker 执行
  → crates/agent-core：一次 apply
  → crates/runtime-nerdctl：受控调用 nerdctl
  → containerd / runc / CNI：实际运行容器
```

`apps/agent/src/bin/agentd.rs` 负责把这些 Rust 实现组装起来。`agent-store-sqlite` 保存任务、配置和运行意图；`node-resources` 为部署准备卷与密钥文件。Go 不直接改 SQLite，网页不直接访问 containerd。

## 一次域名绑定经过哪里

```text
Web 域名面板 → TS SDK → Go HTTP → Go SDK → Rust HTTP
  → ingress：保存域名意图、检查编辑版本、协调目标
  → AgentApi 内部目标解析 → runtime-nerdctl：核对项目容器地址
  → ingress::caddy：生成受限代理配置 → Caddy
```

`ingress-model` 定义域名与 Proxy 契约；`ingress` 的协调部分通过 Proxy 接口工作，Caddy 适配器由可选 feature 启用。证书握手检查由 Go 的 `certificates` 完成；签发与续期由 Caddy 完成。发布工具只负责把 Caddy 安装成系统服务。

## 按修改目标找文件

| 想修改什么 | 从这里开始 |
| --- | --- |
| 部署表单/模板、应用列表 | `apps/web/src/features/apps/` |
| 域名配置页面 | `apps/web/src/features/ingress/` |
| TS 请求与错误处理 | `packages/ts/control-client/src/` |
| 管理 HTTP 路由与认证 | `apps/controlplane/httpapi/` |
| Go ↔ Agent 通信 | `packages/go/agentapi/` |
| Agent 程序配置与组装 | `apps/agent/src/bin/agentd.rs` |
| 应用配置校验/指纹 | `crates/app-model/` |
| 部署逻辑/恢复逻辑 | `crates/agent-core/`、`crates/agent-core/src/recovery.rs` |
| 单 worker 调度与持久化任务 | `crates/agent-core/src/service/`、`crates/agent-store-sqlite/` |
| 容器参数/网络/日志/探测 | `crates/runtime-nerdctl/src/` |
| 卷与密钥文件 | `crates/node-resources/` |
| 域名状态/Caddy 适配 | `crates/ingress/`、`crates/ingress/src/caddy.rs` |
| Ubuntu 安装与升级 | `distribution/installer/` |
| 构建 `.run` 与上游版本锁 | `distribution/release/` |

## 合约、样例、测试与产物

- `contracts/`：两份 OpenAPI。协议变更时同步 Rust DTO、Go SDK、TS SDK 和调用方；目前不是由一个 schema 自动生成全部代码。
- `examples/`：部署配置和可运行四服务示例。示例 Go 服务是独立模块，不是控制端的一部分。
- `tests/`：浏览器与安装器跨模块测试；Rust 测试也在各 crate/app，Go 测试跟随包，TS SDK 测试跟随库。
- `distribution/tests/`：有系统权限要求的 Linux 集成环境与运行时验收，不混入普通单元测试。
- `target/`：本地构建、发布包、缓存和诊断产物，不作为源码提交。根 `Cargo.lock`、`package-lock.json`、运行时锁文件则属于可重复构建的输入，应提交。
- `sources/`：当前聊天项目的只读参考镜像，不参与源码、打包或 Git 提交。

## 为什么根目录有三种 workspace 配置

| 文件 | 管理范围 |
| --- | --- |
| `Cargo.toml` / `Cargo.lock` | Rust 应用与 crates |
| `go.work` / 各模块的 `go.mod` | Go 控制端与 Go SDK |
| `package.json` / `package-lock.json` | Web 与 TS SDK 的 npm workspace |
| `Makefile` | 跨语言开发、检查和构建入口 |

阅读路线：根 README → 本地图 → 对应目录 README → 需要的功能指南。具体依赖关系与权限语义看 [architecture.md](architecture.md)；开始开发看 [CONTRIBUTING](../CONTRIBUTING.md)。
