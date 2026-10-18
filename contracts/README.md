# API 合约

- [agent.openapi.json](agent.openapi.json)：Rust Agent 私有 Unix HTTP API。
- [control.openapi.json](control.openapi.json)：Go 管理 HTTP API，供 Web/TS SDK 使用。

合约描述程序间的数据交换，不包含运行时执行或数据库实现。当前 Rust/Go/TS 类型手工维护，修改协议时同步实现、SDK 和跨语言测试，不假设 OpenAPI 会自动生成代码。

应用部署使用持久化任务/请求键，域名配置使用独立 revision；不要把两个状态流程混在一起。详细语义见 [本地 API](../docs/local-api.md)、[域名指南](../docs/ingress.md)。
