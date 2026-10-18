# 示例与配置

| 示例 | 用途 |
| --- | --- |
| `http-service.toml` / `http-service.apply.json` | 独立 Agent / Go API 的普通 Web 应用请求 |
| `postgres.toml` / `redis.toml` | 卷、数字用户和密钥文件引用 |
| `ai-api.toml` | 引用外部 AI API 的镜像配置，不包含实际密钥或 GPU 调度 |
| `go-client/` | 用 Go SDK 调用本地 Agent |
| `fullstack/` | 前端、后端、PostgreSQL、Redis 的配置生成器与示例服务 |

示例不是默认自动部署的业务。镜像需要与服务器 CPU 架构匹配，密钥由你在对应项目创建。`fullstack/service` 是独立 Go 1.26 模块，在开发机/CI 构建；目标小服务器只导入/拉取镜像。

[四服务使用指南](../docs/fullstack.md) · [卷和密钥](../docs/stateful.md) · [从安装到 HTTPS](../docs/tencent-ubuntu-walkthrough.md)
