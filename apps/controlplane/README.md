# Go 控制端

产物为 `controld`：带令牌认证的管理 HTTP API，同源提供 Web 静态资源，通过私有 Unix Socket 管理本节点 Agent。

| 目录 | 职责 |
| --- | --- |
| `cmd/controld/` | 配置与组件组装、启动、退出 |
| `config/` | 监听地址、令牌和参数校验 |
| `httpapi/` | 路由、认证、请求/响应边界 |
| `control/` | 面向 Agent 接口的管理编排 |
| `certificates/` | 固定本地入口的 TLS 证书验证 |
| `webui/` | 静态页面服务适配 |

独立 Go 模块，由根 `go.work` 与 [Agent SDK](../../packages/go/agentapi/README.md) 组合。Go 不访问 Agent SQLite，也不直接操作容器。

从仓库根执行 `make go-build go-test go-vet`；跨语言集成执行 `make go-integration`。

[控制端操作指南](../../docs/controlplane.md) · [OpenAPI](../../contracts/control.openapi.json)
