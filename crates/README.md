# 9 个 Rust 组件

这些库由根 Cargo workspace 管理，编译进 Agent 或其他使用方，不分别运行进程。13 个旧 crate 的评估与合并原因见 [模块审查](../docs/crate-review.md)。

| Crate | 职责与保留理由 |
| --- | --- |
| `app-model` | 应用配置、资源/网络/挂载校验与指纹；保持不依赖 I/O |
| `runtime-api` | 运行时与资源准备端口；业务流程可替换运行时 |
| `runtime-nerdctl` | 具体容器/网络/日志/探测适配；可脱离 Agent 使用 |
| `agent-api` | DTO/AgentApi/任务仓库端口；可选 Unix HTTP client/server |
| `agent-core` | apply、恢复、单 worker；内部模块划分流程，不绑定 SQLite/nerdctl |
| `agent-store-sqlite` | 存储端口实现、迁移与租约；可替换为其他存储 |
| `node-resources` | 卷/密钥的文件系统和权限策略，供不同运行时/宿主组合复用 |
| `ingress-model` | 域名类型、状态、Proxy 端口；避免 API 与协调服务互相依赖 |
| `ingress` | 域名协调/持久化，可选 Caddy 适配器 |

## 不需要新 crate 的内部模块

`agent-core/src/apply.rs` 和 `store.rs` 是部署流程/存储端口，`recovery.rs` 是一次恢复协调，`service/` 是任务与恢复排序。原来的三份 Cargo 包已移除，没有保留平行实现或转发 crate。

`agent-api/src/client.rs` / `server.rs` 是同一协议的可选传输；`ingress/src/caddy.rs` 是同一个域名组件的可选代理适配。bootstrap 仍只有 [一份源文件](ingress/bootstrap.json)。

## 按需复用

```toml
# 只需要协议类型，默认不包含 HTTP/SQLite/nerdctl。
agent-api = { path = "../deploy-platform/crates/agent-api", default-features = false }
# Rust 客户端可改为：features = ["client"]
# 提供 Unix 服务可改为：features = ["server"]

ingress = { path = "../deploy-platform/crates/ingress", default-features = false }
# 使用 Caddy 时添加 features = ["caddy"]；否则实现 ingress-model::Proxy。
```

`ingress` 自身包含 SQLite 域名存储；不启用 caddy 仅表示不引入该代理的 HTTP 适配。`agent-core` 的业务流程不包含具体存储/运行时实现。Cargo workspace 同时构建多个使用方会合并 feature，因此独立组合检查使用 `cargo check -p agent-api --no-default-features` 等命令，已纳入 `make rust-check` 与 CI。

阅读路线：`app-model` → `runtime-api` → `agent-core` → `apps/agent` 的组合入口。当前库未发布公共包仓库；复用先采用路径或固定 Git revision。
