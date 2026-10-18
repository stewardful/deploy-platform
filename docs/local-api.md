# 本地管理 API v1

agentd 是节点状态的唯一写入者。agentctl 仅通过 HTTP/1.1 + JSON Unix Socket 管理；不再接受 `--nerdctl`、`--address`、`--namespace`、`--state-dir`。这些参数只属于 agentd。validate/plan 仍完全离线。需要升级原有调用脚本。

## 端点

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| GET | `/v1/health` | API 版本、服务版本、worker 是否可接收任务；不探测 containerd/业务健康 |
| GET | `/v1/apps` | 持久化应用摘要、意图、阶段、恢复退避；不含配置值 |
| GET | `/v1/operations?limit=20` | 最近 apply 操作，最新在前；limit 1..1000 |
| POST | `/v1/jobs` | 202 + 已持久化任务，包含 ID；不等待容器操作 |
| GET | `/v1/jobs/{id}` | 任务状态、结果或错误；任务失败仍返回 HTTP 200 |
| GET | `/v1/diagnostics/{project}/{name}` | 容器状态、项目服务名、按需 HTTP 健康快照 |
| GET | `/v1/logs/{project}/{name}?tail=100` | 受限应用日志，1..500 行、64 KiB、无 follow |

正式字段与约束见 [OpenAPI](../contracts/agent.openapi.json)。GET 列表暂不分页，响应最大 1 MiB，超过返回 500；适用于小型节点。apply 的 `spec` 对应 app-model AppSpec 的 JSON，不是 TOML 字符串。未知请求字段会拒绝；客户端应容忍新增的响应字段。Go 使用 int64 接收任务/操作 ID 和毫秒时间。

## 提交与查询

```bash
sudo curl --unix-socket /run/deploy-platform/agent.sock \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"stop-demo-web-1","command":{"kind":"stop","app":{"project":"demo","name":"web"}}}' \
  http://localhost/v1/jobs
sudo curl --unix-socket /run/deploy-platform/agent.sock http://localhost/v1/jobs/1
```

命令类型：`apply {spec, startup_wait_ms}`、`start {app}`、`stop {app}`，均放在 `command` 中并带 `kind`。request_id 必填，1..128 位 ASCII 字母、数字、下划线或横线。CLI 默认自动生成，也可显式提供。

任务 `queued → running → succeeded/failed`。默认串行执行，最多 32 个 queued + running，可由 agentd `--queue-capacity 1..128` 调整。任务和应用意图分别落库：202 保证任务已保存，worker 执行到该任务时才保存应用意图。恢复与任务共享同一 worker；慢拉取可能延迟后续 stop，但不阻塞状态查询。

幂等键在该状态库内永久唯一。同键同规范化命令返回原任务（包含终态）；同键不同命令返回 409。新提交队列满时 429；已有键即使满队列也可查询式重提。恢复依赖实际容器状态核对，不承诺底层运行时操作的 exactly-once。配置变更和归属冲突不会自动替换容器。

客户端断线/等待超时不取消任务。已知 ID 用 job 查询；提交结果不确定则原键、原命令重试。CLI 错误会提供 request_id 或已知 job_id。失败终态不会因重提旧键而重新执行；重新尝试需要新键，且 running 意图可能已由自动恢复处理成功，应先查看 apps。start/stop 动作失败也会保留已保存意图，由恢复重试。

SIGTERM 会取消当前运行时等待，未完成任务保留；重启后 running 任务回到 queued，先处理启动时已存在的 FIFO 队列，再自动恢复，避免排队的停止被绕过。队列接收和恢复检查之间不持有长 SQLite 事务。

错误 JSON：`{"code":"queue_full","message":"agent job queue is full","retryable":true}`。HTTP 400 输入错误，404 不存在，409 幂等键冲突，413 超大请求，415 非 JSON，429 队列满，500 内部错误，503 worker/请求超时。Job.error 使用同一结构；业务执行冲突在 Job 内表达。retryable 表示故障可能暂时存在，不表示旧终态任务会重跑。HTTP 解析器级错误可能在进入 JSON 路由前直接关闭连接。

## 权限与资源

默认 `/run/deploy-platform/agent.sock`，父目录 0700、socket 0600；没有 TCP 监听，也没有额外 token。访问私有目录/Socket 的本机账户必须可信。同节点 Go 控制端已通过可信 Socket 访问 Agent，并在回环 HTTP 管理入口校验 Bearer token。不要把 Socket 改成全员可写。业务域名 HTTPS 由独立代理提供；远程 Agent 传输和多用户授权尚未实现。

Socket 必须绝对路径；拒绝不安全父目录、普通文件/符号链接和活动 Socket。独立 socket 租约防止不同状态目录绑定同一路径；只清理确认拒绝连接的旧 Socket，退出仅删除自己持有的 inode。目录及祖先路径必须由管理员控制。

HTTP 最多 32 个活动连接，超出直接断开；头部上限 16 KiB / 32 项，头部与请求处理各 5 秒，正文 256 KiB，响应 1 MiB，不保持空闲长连接。元数据查询使用最多 2 个阻塞线程；worker 使用独立当前线程执行器串行运行。没有 Redis/消息队列外部服务。资源占用需在 2 核 2 GB 节点实测，历史任务和操作暂不自动清理。

## 复用与 Go 示例

- agent-api：默认提供 DTO、异步 AgentApi、同步 JobRepository；默认不引入 HTTP、SQLite 或运行时实现。
- agent-core::service：泛型 ContainerRuntime + DeploymentStore + JobRepository；不依赖具体数据库/容器工具。
- agent-api 的 client/server feature：外部 Rust 程序可只启用一侧，独立嵌入。

[Go 示例](../examples/go-client/main.go) 已复用独立 Go SDK（packages/go/agentapi），通过根 go.work 解析本地模块，无第三方 Go 库，不依赖 Rust 或共享数据库；从仓库根目录运行：

```bash
sudo go run examples/go-client/main.go -socket /run/deploy-platform/agent.sock
sudo go run examples/go-client/main.go -socket /run/deploy-platform/agent.sock -submit request.json
sudo go run examples/go-client/main.go -socket /run/deploy-platform/agent.sock -job 1
```

示例只演示 SDK 的调用；完整控制端位于 apps/controlplane。调用方应保存 request_id 与 Agent job_id，不在超时后生成新键重投，不访问 Agent SQLite。

0.2.0 增加独立密钥写入、密钥名称列表与卷元数据接口，支持 agentctl secret-create/secrets/volumes。服务接口有默认不可用实现，未组装资源组件的嵌入使用方仍可只实现原任务 API，详见 [有状态指南](stateful.md)。

0.3 项目网络、HTTP 探测的目标限制与日志边界详见 [完整部署指南](fullstack.md)。

0.4 新增持久化域名 API，含查询、版本化编辑、删除和 Go 本机证书检查，见 [域名与 HTTPS](ingress.md)。
