# Agent v1 Go SDK

独立 Go module，只有标准库依赖。只通过本地 Unix Socket 访问 Agent，不读取状态库或操作容器。当前模块路径是本仓库本地标识，尚未发布。

```go
client, err := agentapi.NewClient("/run/deploy-platform/agent.sock", 5*time.Second)
if err != nil { return err }
defer client.Close()
apps, err := client.Applications(ctx)
```

通过 AppSpec + Command + Submission 调用 Submit，保存客户端 request_id 和返回的 job ID。请求超时不代表未接受；不自动重试，不要换键重投。Job/Wait 只查询任务，Wait 的上下文取消不取消部署；失败任务通过 Job.State/Error 表达。

支持 Health、Applications、Operations、Submit、Job、Wait，以及密钥/卷、诊断/日志、域名查询与修改；类型和输入限制对应 [Agent OpenAPI](../../../contracts/agent.openapi.json)。Job.Result 保留原始 JSON，可按 kind 解码为 AppliedResult / IntentResult。响应允许新增字段。

其他 Go 项目复用时，使用 go.work use 或 replace deploy-platform.local/agentapi => 本地 SDK 路径；正式发布时替换为真实仓库路径并固定版本。独立模块编译不依赖根 go.work。SDK 的契约测试读取本 monorepo 的 OpenAPI，运行这项测试需要保留相对目录。

详细使用和部署见 [Go 控制端](../../../docs/controlplane.md)、[本地 API](../../../docs/local-api.md)。
