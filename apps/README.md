# 应用入口

这里放最终运行的程序，负责配置、组装和生命周期。通用能力放在 `crates/` 或 `packages/`，避免在多个入口复制实现。

| 目录 | 产物 | 从哪里读 |
| --- | --- | --- |
| [agent](agent/README.md) | Rust `agentd`、`agentctl` | `src/bin/agentd.rs` 组装；`src/main.rs` 命令行 |
| [controlplane](controlplane/README.md) | Go `controld`，也托管 Web 静态资源 | `cmd/controld/` |
| [web](web/README.md) | React 静态页面 | `src/App.tsx` 与 `src/features/` |

Web 在生产由 Go 提供静态资源，不需要独立 Node.js 进程。Rust 各 crate 也不是独立常驻服务。节点额外运行 containerd 与 Caddy。

[返回项目地图](../docs/project-map.md)
