# 项目网络、日志、HTTP 检查和完整应用部署

本文适用于 0.5.0，介绍自 0.3 引入的项目网络及诊断能力。部署节点使用 Go 控制端 + 独立 Rust Agent + nerdctl + containerd，不需要 Docker、Compose、Node.js 或编译器。

## 已实现的边界

- `network: "project"` 为项目创建 `dp-project-<project>` 网络，应用名成为同一项目内的服务名，例如 `backend:8080`、`postgres:5432`、`redis:6379`。不同项目可以使用相同应用名。
- 复用 nerdctl 2.4.1/CNI 的 IPAM、bridge 和 firewall `same-bridge`，先验证网络归属标签与防火墙配置，拒绝接管同名外部网络。网络保留在 nerdctl 的持久化配置中，Agent 不维护第二套 IP 分配器。
- 服务名由 nerdctl 自动维护的 `/etc/hosts` 提供，容器创建/删除后更新同网络成员。不提供通用 DNS 服务器。应用要使用系统解析器；自行向 DNS 查询服务名、只在启动时缓存 IP 的应用需自行适配或重启。
- `bridge` 保留原有共享网络语义，旧配置与指纹不变；已有应用配置不可原地变更，需要新应用名部署。`none` 保留无网络语义。
- 只有显式端口映射才绑定宿主机 **127.0.0.1**；数据库/Redis 通信不需要映射端口。公网访问可配置 [域名、反向代理与自动 HTTPS](ingress.md)。
- 不同桥接项目之间由 CNI 防火墙限制转发。这是单管理员、可信镜像场景下的项目分组，不是恶意多租户的强隔离；宿主机、root、共享内核和容器主动出站网络仍属于原有信任边界。

## 模块

| 模块 | 职责 |
|---|---|
| `app-model` | 项目网络、HTTP 检查与拉取策略的可序列化配置与校验 |
| `runtime-api` | 独立的诊断/日志 DTO 与可选运行时能力；不依赖 HTTP 或数据库 |
| `runtime-nerdctl/network.rs` | 归属验证、网络创建，复用上游 CNI |
| `runtime-nerdctl/diagnostics.rs` | 只访问已验证容器 IP 的受限 HTTP 检查 |
| `agent-core::service` | 读取持久化配置、核实应用存在、最多两个并发诊断请求；部署继续串行 |
| `agent-api` | HTTP + JSON Unix Socket 合约与传输 |
| `packages/go/agentapi` | 可独立复用的 Go SDK；控制端不执行容器命令 |
| `packages/ts/control-client` | 独立 TypeScript SDK |
| Web `DiagnosticsPanel` / `SpecImport` | 诊断、日志、导入配置，内容不写浏览器存储 |
| `examples/fullstack/generate.py` | 无副作用的四服务配置生成器 |

## HTTP 健康检查

```json
{"network":"project","health_check":{"port":8080,"path":"/health"}}
```

端口为容器端口，无需开放宿主端口。仅允许以 `/` 开头的有限 ASCII 路径，不允许 URL、主机、查询参数、头部、命令或凭证。检查前核实容器归属/配置指纹、项目网络及隔离策略，从运行时检查结果读取该网络的私有 IPv4 地址，直接连接，不使用 DNS/代理、不跟随重定向、不读取响应正文，超时 2 秒、响应头最多 8 KiB/32 项。只有 2xx 为健康。

这是**按需 HTTP readiness 快照**：CLI/API 调用时检查，网页诊断面板打开时每 10 秒刷新，关闭后停止。区分 `healthy`、`unhealthy`、`not_running`、`not_configured`，返回检查时间与 HTTP 状态。诊断失败会返回错误而不是伪造健康状态。

部署任务成功仍表示容器启动成功，不代表 HTTP 就绪。第一版不把 HTTP 不健康作为自动重启条件，避免数据库暂时故障造成重启风暴。Agent 原有进程退出恢复保持有效；没有关闭网页后的后台健康告警、历史监控或依赖编排调度。

## 日志

读取前核实已登记应用与容器标签，只用不可变容器 ID 调用 `nerdctl logs`，不接受路径、命令或 follow。默认 100 行、1..500 行，2 秒超时，输出总计最多 64 KiB，超限返回错误并提示减少行数。stdout 与 stderr 按流分组拼接，**不保证两路间的全局时间顺序**。保留时间戳，移除终端控制字符，网页使用纯文本渲染。

日志来自应用本身，应用仍可能主动打印秘密；平台无法保证日志自动脱敏。不要把密码放进启动参数或普通环境变量，密钥使用 `/run/secrets/` 文件。管理令牌有节点管理权限，目前没有项目级多用户授权。

## API / CLI

Unix Socket Agent 路径：

- `GET /v1/diagnostics/{project}/{name}`
- `GET /v1/logs/{project}/{name}?tail=100`

Go Web API 对应 `/api/v1/diagnostics/...`、`/api/v1/logs/...`，沿用管理令牌认证和响应大小限制。

```sh
sudo cloudctl diagnostics --project fullstack --name backend
sudo cloudctl logs --project fullstack --name backend --tail 50
```

Web 应用列表点击“诊断”，可查看容器状态、项目网络、服务名、HTTP 健康和日志。部署表单支持项目网络、健康检查、镜像拉取策略、导入应用 JSON；PostgreSQL/Redis 模板默认仅项目内通信。

## 构建示例镜像（在开发机/CI）

示例前端提供静态页面并代理 `/api/` 到 `backend:8080`；后端使用 PostgreSQL 持久计数、Redis 写入读取缓存，`/health` 同时检查数据库与 Redis。两个角色复用一个很小的静态 Go 二进制镜像。数据库连接池最大 2，Go GOMEMLIMIT 和各容器内存限制均已设置。

Go 示例是独立模块，使用 Go 1.26 工具链；控制端现有 Go 工具链不变。

```sh
mkdir -p target/fullstack-build
GOWORK=off GOTOOLCHAIN=go1.26.9 CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go -C examples/fullstack/service build -trimpath -ldflags='-s -w' \
  -o ../../../target/fullstack-build/fullstack-demo .
cp examples/fullstack/service/Dockerfile target/fullstack-build/Dockerfile
docker build --platform linux/amd64 -t REGISTRY/PROJECT/fullstack:v1 target/fullstack-build
docker push REGISTRY/PROJECT/fullstack:v1
```

ARM64 服务器将 GOARCH 和镜像平台改为 arm64。正式部署推荐固定镜像 digest。示例数据库连接在项目私有网络内使用密码认证，未配置数据库传输 TLS，不适用于跨主机数据库连接。

## 网页部署步骤

1. “数据与密钥”在同一个项目创建 `db-password`、`redis-password`、`redis-config`。`redis-config` 内容为 `requirepass "<与 redis-password 相同的密码>"` 加换行；示例密码应使用至少 32 位随机十六进制字符。秘密不能放入 JSON 应用描述。
2. 生成四份 JSON：

   ```sh
   python3 examples/fullstack/generate.py --project fullstack \
     --image REGISTRY/PROJECT/fullstack:v1 --output target/fullstack-specs
   ```

3. 网页“部署应用 → 导入应用 JSON”，按 `postgres`、`redis`、`backend`、`frontend` 顺序逐个提交；等待后端诊断显示健康。
4. 只有前端映射本机 18080。通过 SSH 转发访问：

   ```sh
   ssh -L 18080:127.0.0.1:18080 USER@SERVER
   ```

5. 浏览器打开 `http://127.0.0.1:18080` 点击写入，返回 PostgreSQL 持久计数与 Redis 读取值。
6. 停止数据库后查看后端诊断，应显示 HTTP 503、日志 `dependency unavailable`；启动数据库后恢复。服务之间使用应用名，不填写容器 IP。

正式镜像放在可访问的镜像仓库中。私有仓库凭据管理尚未提供。如需离线使用：先通过可信渠道导入镜像，再设置 `pull_policy: "never"`。`always` 是默认行为，`if_not_present` 优先已有镜像；Agent 不承担构建与上传镜像。

```sh
sudo cloudctl runtime load -i fullstack-demo.tar
```

## 真实节点验收

安装当前版本后，在专门测试节点运行：

```sh
sudo cloudctl test fullstack --image REGISTRY/PROJECT/fullstack:v1
# 已导入自建镜像时：
sudo cloudctl test fullstack --image docker.io/library/cloud-fullstack-demo:v1 --pull-policy never
# 需要留下网页演示：追加 --keep-running
```

验收通过 Web API 部署四服务，写入读回、停止 PostgreSQL 验证 HTTP 503 与日志、恢复数据库、停止 Agent 并移除其测试后端实例、重新启动 Agent 后验证自动重建和服务名继续可用；创建第二项目确认服务名和直接 IP 不能跨项目访问。会重启 Agent，请在专门测试节点使用。默认停止测试应用，保留数据、密钥与项目网络，不执行清理数据库/网络的操作。

2 核 2G 分配：PostgreSQL 384 MiB、Redis 256 MiB、后端 128 MiB、前端 64 MiB；这些是配置上限，不是实测总内存保证。数据库参数适合小型开发/个人应用，不代表高并发生产容量。额外数据、镜像和日志仍需实际磁盘容量。

## 为什么这些边界放 Rust

Rust 适合本节点高权限执行边界：结构化配置、容器/网络归属、参数构造、文件资源边界、有限日志读取、探测目标限定与超时取消。Go 保持 API 编排与认证，React 保持展示；不重复实现同一套容器控制。复用成熟 containerd/runc/CNI，比自行重写运行时、DNS 或防火墙更容易验证。Rust 的内存安全不会自动解决授权、SSRF、秘密泄露或错误的防火墙策略，仍需明确校验和真实负面测试。

参考：[nerdctl 2.4.1 hosts 更新逻辑](https://github.com/containerd/nerdctl/blob/v2.4.1/pkg/dnsutil/hostsstore/updater.go)、[CNI 网络实现](https://github.com/containerd/nerdctl/blob/v2.4.1/pkg/netutil/netutil_unix.go)。
