# 13 个 crate 的审查与 9 个组件方案

本次以“确有替换/独立复用需求”为包边界，把同一能力中的流程拆分降为内部 Rust module。目标是减少 Cargo 包、依赖声明和目录跳转，同时保持一个实现来源、独立 Agent 和可裁剪的 SDK。

## 每个原模块的结论

| 原 crate | 结论 | 原因 |
| --- | --- | --- |
| app-model | 保留 | 纯配置与校验可独立使用，不应引入数据库和运行时 |
| runtime-api | 保留 | engine、资源、诊断使用同一端口；不强迫使用 nerdctl |
| runtime-nerdctl | 保留 | 有实际命令、进程、网络、归属检查，是可替换后端 |
| agent-engine | 合入 agent-core | apply 与 worker/恢复共享存储端口，无需独立发三个版本 |
| agent-reconciler | 合入 agent-core::recovery | 只是同一部署状态模型的一次恢复算法 |
| agent-service | 合入 agent-core::service | 调度 apply 和 recovery 的单 worker，不是另一个服务进程 |
| agent-store-sqlite | 保留 | 具体持久化、迁移与锁，独立于流程以便替换 |
| agent-api | 保留并收进 HTTP | DTO、端口与传输围绕同一协议，传输可按 feature 裁剪 |
| agent-http | 合入 agent-api 的 client/server | 两份 Cargo 配置的维护价值有限；仍保留客户端/服务端边界 |
| node-resources | 保留 | 文件权限、卷/密钥生命周期独立于 DB 与容器后端 |
| ingress-model | 保留 | Agent 协议与域名协调都依赖它；若合入 ingress 会形成 API/服务循环依赖 |
| ingress-service | 改为 ingress | 域名能力统一入口，保留协调/持久化 |
| ingress-caddy | 合入 ingress::caddy | 当前只有一个适配器，用 feature 保留可替换性，无需单独发包 |

旧 crate 目录、依赖和入口均移除，没有为了兼容名字添加空壳 crate。恢复与 SQLite 的联合测试归入 `agent-store-sqlite/tests/`，`agent-core` 不为测试反向依赖具体存储。

## 合并后仍独立的边界

```text
runtime-nerdctl → runtime-api → app-model
ingress-model → app-model
agent-api → app-model + runtime-api + ingress-model
agent-core → agent-api + runtime-api + app-model
agent-store-sqlite → agent-core + agent-api
node-resources → agent-api + runtime-api + app-model
ingress → agent-api + ingress-model + app-model
apps/agent → 组装具体实现，输出 agentd / agentctl
```

图只表达主要依赖；实际 Cargo 依赖以各清单为准。`ingress-model` 只定义 Proxy 端口；`ingress` 的 Caddy feature 启用具体适配。API 仅有 DTO 时不包含 HTTP；启用 client 不强迫提供 server。

## 命令默认能力

安装后 `cloudctl` 默认展示分类帮助，直接支持应用列表/部署/启停、诊断/应用日志、任务、密钥/卷和域名。这些操作都转发到同一个 agentctl/API，不经过 Go/Web，不直接写状态库。

- `cloudctl apply` / `deploy` 默认等待持久化任务结果；`--no-wait` 才只提交。
- `cloudctl logs --project P --name APP` 默认 100 行；`system-logs` 明确表示节点服务日志。旧 `logs agent|control|runtime|proxy` 保留兼容。
- `agentctl` 自身保留全部操作与 `help`；`list/deploy/status` 是 apps/apply/doctor 的可见别名，没有第二套流程。
- apply/validate/plan 支持 TOML 和普通 AppSpec JSON，共用同一个 AppSpec 校验和指纹。JSON 请求信封如 `http-service.apply.json` 仍只用于 Go HTTP 提交，不能当作普通配置导入。
- 不隐式创建数据库、不自动部署模板、不默认开放端口或执行清理。不配置 project/name 的启停请求仍拒绝，避免操作错误应用。

详细用法见 [命令指南](commands.md)。API schema、任务/幂等与状态目录保持原语义；源码 crate 路径属于未发布的接口调整，版本更新为 0.5.0。

## 验证

2026-10-18，本轮通过：

| 验证 | 结果 |
| --- | --- |
| Rust workspace | 71 项通过，默认忽略的 Linux 专项另行执行通过 |
| 组件裁剪 | 协议-only、client-only、server-only，ingress 无 Caddy/有 Caddy 均通过；依赖树确认协议-only 无 Hyper/Tokio/SQLite/nerdctl |
| 工具链与质量 | Clippy、fmt、Linux Rust 1.85 检查通过；外部依赖版本和摘要未变化 |
| Go / Web | 无缓存 Go race/真实 Rust Socket 集成、vet、TS 8、浏览器 3、生产构建通过 |
| 命令与安装组件 | 10 项安装组件测试；含参数完整转发、别名默认行为、未知命令失败、help 不触发自检 |
| ShellCheck | 统一命令与安装/发布脚本静态检查通过 |
| 2 CPU/2 GiB Ubuntu ARM64 | 全新安装、重复安装、数据库持久化、自检后重装通过 |
| 0.4.1→0.5.0 | 对比令牌/密钥/卷元数据摘要与域名版本，HTTPS/SQL/Redis 数据保留 |
| Agent 独立能力 | 实际停止 Go/Web，通过统一命令部署 JSON、查询任务/诊断/日志/资源/域名、启停；原 HTTPS 数据链路继续可用 |
| HTTPS | 隔离本地 CA 的真实签发/自动续期、SNI/Host 校验、503/恢复、代理/后端重建通过 |
| 安装包 | 两架构 ELF/文件清单、CLI/Web/bootstrap 比对与 SHA256 通过；真实安装在 ARM64，AMD64 未做云节点运行验收 |

Linux 专项 fixture 也明确使用自己的无代理 Socket，不让测试 namespace 的临时 daemon 修改已有节点代理。普通应用恢复测试与域名代理测试的运行目标因此隔离。

本轮日志为本地 `target/consolidation-*.log`，不提交 Git。测试 CA 仅在可删除容器内受信，特殊 CA 与测试 hosts 不进入发布默认配置。没有验证用户腾讯云公网签发、真实服务器整机重启、备份恢复或长期容量；这些仍按上云手册执行。历史 0.4.1 记录单独保留。
