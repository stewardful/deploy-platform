# Go 单节点控制端

第一版提供可部署的控制 HTTP API。它通过 Unix Socket 管理同节点 Rust Agent，适合管理预先构建好镜像的前端、后端和有状态应用。已增加 React/TypeScript/TDesign 工作台，安装见 [Web 指南](web.md)。没有 Git 源码构建、远程节点、多用户权限、应用更新和回滚；原有 Agent 的限制继续适用。

## 模块与依赖

```text
go.work
├── packages/go/agentapi        独立 Go module：v1 类型、校验、Unix Socket SDK
└── apps/controlplane           独立 Go module
    ├── control                业务编排，只依赖 Agent 接口
    ├── httpapi                Bearer 认证、HTTP 路由、大小/并发边界
    ├── config                 管理员配置、私有令牌文件检查
    └── cmd/controld            组装、HTTP 生命周期、信号退出
```

Go 只使用标准库，无第三方 Go 依赖，不连接 containerd 或 SQLite。Agent 保持唯一配置、任务、部署和恢复拥有者；控制端重启不影响运行中的应用或任务。没有新增数据库、队列服务或每应用 worker。

Go module 当前路径为本仓库的本地标识 `deploy-platform.local/agentapi`，尚未发布。其他项目可通过 go.work use 或 go.mod replace 指向 SDK；正式发布时统一替换为实际仓库模块路径并打版本。control、httpapi 也可独立嵌入其他 Go 程序，HTTP 层接受 Manager 接口，不要求具体 Agent 实现。

## 构建与启动

根目录使用 Go workspace（最低 Go 1.23，本地检查使用 Go 1.25.5）：

```bash
make go-build
# 帮助，不需要 token 文件或启动 Agent
./target/controlplane/controld --help
```

先按 [安装指南](install.md) 或 [独立 Agent 指南](agent.md) 准备运行时和 agentd。Go 构建无需 CGO，可在开发/CI 机器构建目标架构产物，避免在 2 GB 节点编译 Rust：

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath \
  -o target/controlplane/controld-linux-amd64 ./apps/controlplane/cmd/controld
# ARM64 节点改 GOARCH=arm64
```

新节点推荐 [Ubuntu 一键安装](install.md)。以下为手动部署方式，不能覆盖一键安装器管理的 unit；在手动管理的目标 Ubuntu 安装对应架构二进制、创建私有 token，并启动：

```bash
sudo install -d -m 0700 /etc/deploy-platform
# 只在首次安装创建 token；不要覆盖已有 token
sudo sh -c 'umask 077; set -C; openssl rand -hex 32 > /etc/deploy-platform/control.token'
sudo install -d -m 0755 /usr/local/share/deploy-platform/web
# 先在开发/CI 机器 npm ci && make web-build，并上传 apps/web/dist
sudo cp -R apps/web/dist/. /usr/local/share/deploy-platform/web/
sudo install -m 0755 target/controlplane/controld /usr/local/bin/controld
sudo install -m 0644 distribution/manual/systemd/deploy-control.service /etc/systemd/system/deploy-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now deploy-control.service
sudo systemctl status deploy-control.service
```

跨平台构建时，上述 install 的源路径换成已上传的对应架构 Linux 产物。当前 macOS 二进制不能直接用于 Ubuntu。systemd 模板默认同时提供网页；纯 API 部署可去掉 --web-dir。一键安装使用另一套专属运行时 unit，验证范围见安装指南。

前台运行：

```bash
sudo ./target/controlplane/controld \
  --listen 127.0.0.1:8088 \
  --agent-socket /run/deploy-platform/agent.sock \
  --token-file /etc/deploy-platform/control.token \
  --web-dir "$PWD/apps/web/dist"
```

token 文件必须为私有普通文件（0600 或更严格），不接受符号链接，内容 32..128 位 ASCII 字母、数字、下划线、横线；不通过命令行参数传 token。替换 token 后重启服务，旧 token 立即失效。默认 Agent socket 为 root 0600，因此示例 Go 服务同样由 root 运行，仅用于可信管理员单节点场景。

控制端只允许显式 loopback IP 监听，默认 `127.0.0.1:8088`，拒绝 `0.0.0.0`、公网 IP 和主机名。所有端点（包括 health）都要求 `Authorization: Bearer <token>`。暂不开放公网；远程使用 SSH 本地端口转发，例如 `ssh -N -L 8088:127.0.0.1:8088 user@server`，在本机安全地提供同一 token。不要在腾讯云安全组开放 8088。

## 部署和管理自己的项目

先把项目构建为 Linux 节点架构的镜像并推送可访问的仓库；镜像的启动命令、监听端口应事先配置正确。本版不在服务器构建源码，尚未提供私有仓库凭据管理。应用 API Key/数据库密码使用项目密钥文件，不能放入 environment/args；数据库使用应用专属持久化卷，详见 [有状态应用](stateful.md)。

为便于复制测试，以下在目标机器的管理员 shell 中设置令牌；不要开启 shell 调试或将它写入共享日志：

```bash
CONTROL_TOKEN="$(sudo cat /etc/deploy-platform/control.token)"
curl -sS -H "Authorization: Bearer $CONTROL_TOKEN" http://127.0.0.1:8088/api/v1/health
curl -sS -H "Authorization: Bearer $CONTROL_TOKEN" http://127.0.0.1:8088/api/v1/apps
curl -sS -H "Authorization: Bearer $CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/http-service.apply.json \
  http://127.0.0.1:8088/api/v1/apps/apply
```

示例部署 demo/web，返回 202 和 Agent job ID。它不是业务就绪通知。将示例中的 image、app、resources、network、ports 换成自己的配置；默认无网络；bridge/project 的显式发布端口只绑定 127.0.0.1。前后端公网访问使用 project 网络及 [域名与自动 HTTPS](ingress.md)，无需发布宿主端口。

```bash
# 1 替换为返回的真实 ID
curl -sS -H "Authorization: Bearer $CONTROL_TOKEN" http://127.0.0.1:8088/api/v1/jobs/1
curl -sS -H "Authorization: Bearer $CONTROL_TOKEN" \
  -H 'Content-Type: application/json' -d '{"request_id":"stop-demo-web-1"}' \
  http://127.0.0.1:8088/api/v1/apps/demo/web/stop
curl -sS -H "Authorization: Bearer $CONTROL_TOKEN" \
  -H 'Content-Type: application/json' -d '{"request_id":"start-demo-web-1"}' \
  http://127.0.0.1:8088/api/v1/apps/demo/web/start
curl -sS -H "Authorization: Bearer $CONTROL_TOKEN" 'http://127.0.0.1:8088/api/v1/operations?limit=20'
unset CONTROL_TOKEN
```

镜像或配置变化的更新仍由 Agent 明确拒绝，不能覆盖同名应用；先完成后续发布/替换能力再用于持续部署。stop 保存停止意图并停止容器，start 恢复运行意图。启动观察只是进程存活，不等于 HTTP 健康。

## HTTP 契约与任务语义

正式字段见 [控制端 OpenAPI](../contracts/control.openapi.json)，其中响应模型复用 [Agent 契约](../contracts/agent.openapi.json)。

| 方法 | 路径 | 输入/返回 |
| --- | --- | --- |
| GET | `/api/v1/health` | Agent v1 + worker 就绪；不可用返回 503 |
| GET | `/api/v1/apps` | Agent 持久化应用摘要，含运行意图/阶段/退避 |
| POST | `/api/v1/apps/apply` | `{request_id,spec,startup_wait_ms?}` → 202 Job |
| POST | `/api/v1/apps/{project}/{name}/start` | `{request_id}` → 202 Job |
| POST | `/api/v1/apps/{project}/{name}/stop` | `{request_id}` → 202 Job |
| GET | `/api/v1/jobs/{id}` | Job；终态失败仍返回 200，查看 state/error |
| GET | `/api/v1/operations?limit=20` | 最近 apply 操作，limit 1..1000 |

request_id 必填、客户端生成并保存，同一意图重试必须同键同命令。控制端不自动补键或重投，超时不表示 Agent 未接受；503 错误中会保留合法 request_id。提交结果不确定，用原键原请求重试；已知 job ID 则查询。相同键返回原终态，失败任务不会重跑；主动新操作用新键。Agent 继续负责 32 默认队列上限、409 键冲突、429 队列满、任务重启重放与串行变更。

控制端不另存用户请求、job ID 或配置，调用方需保存其关联；无需两端数据库同步。控制端重启不会清除 Agent 的任务。SIGTERM 退出仅关闭控制 API，不取消 Agent 已接受任务。

输入错误 400，缺令牌/错误令牌 401，资源不存在 404，方法错误 405，超大正文 413，非 JSON 415；Agent 错误按原状态返回，协议错误 502，传输不确定/忙 503。错误不回显请求正文或原始网络诊断。所有响应禁止缓存；不提供 CORS。唯一令牌代表完整节点应用管理权限，不是多用户 RBAC。

## 2 核 2 GB 与验证边界

HTTP 最多 16 个并发处理，超出直接 503，不建立无界工作队列。请求正文 256 KiB、响应 1 MiB、头部约 16 KiB；读头 5 秒、读请求 10 秒、空闲连接 15 秒，Agent 单请求默认 5 秒，可设 100ms..60s。写超时为 Agent 超时加 5 秒。SDK 每请求新 Unix 连接、不跟随重定向、禁用代理、不退回 TCP、不自动重试。长任务由 Agent 执行，控制端请求不等待镜像拉取。

systemd 设置 GOMEMLIMIT=64MiB 和 MemoryHigh=96M，均为软目标，不能替代真实空载/部署峰值测量。Agent 与业务容器另算资源；限制应用总资源，在开发机/CI 构建镜像。任务和操作历史仍由 Agent 保存，暂不自动清理。

```bash
make go-vet go-test go-build
# 需 Rust + Go，允许创建本地 Unix/TCP 测试 socket
make go-integration
```

普通 Go 测试覆盖认证、输入边界、超时不重投、错误映射和 SDK 协议限制。跨语言测试通过真实 Go HTTP → SDK → Rust HTTP/service/SQLite，只有容器运行时为模拟实现；未设置 AGENT_API_FIXTURE 时该测试明确跳过。真实 containerd、systemd 和服务器重启测试仍须在 Ubuntu 执行。CI 已配置两个系统的跨语言验证，不代表远端已运行通过。

0.2.0 新增写入型密钥 API、卷元数据 API 与 PostgreSQL/Redis 部署，见 [有状态应用指南](stateful.md)。
