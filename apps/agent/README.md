# Rust Agent

Cargo 包名为 `node-agent`，是可复用 Rust 组件的组合入口。

- `src/bin/agentd.rs`：组装 SQLite、nerdctl、资源管理、域名协调和 Unix HTTP 服务；负责配置、信号及退出。
- `src/main.rs`：`agentctl` 命令行；日常管理通过 Agent API，`validate/plan` 离线执行。
- `src/lib.rs`：入口共享的配置加载等辅助逻辑。
- `examples/api_fixture.rs`：跨语言/浏览器测试使用的 API 服务，运行时为替身，不用于生产。
- `tests/`：CLI/Socket 持久化测试及需显式启用的真实 Linux 运行时测试。

从仓库根目录构建/检查：

```bash
cargo build --locked -p node-agent --bins
cargo run --locked -p node-agent --bin agentctl -- validate -f examples/http-service.toml
```

单 worker、持久化任务、部署与恢复逻辑分别位于 [Rust 组件](../../crates/README.md)。独立 Agent 可以不运行 Go/Web；不要让 agentctl 与 daemon 同时直接写同一状态库。

[手动运行](../../docs/agent.md) · [本地 API](../../docs/local-api.md) · [恢复语义](../../docs/recovery.md)
