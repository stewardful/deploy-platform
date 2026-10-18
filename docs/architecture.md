# 模块边界与维护约定

## 依赖方向

```text
agentctl → agent-api(client) → Unix Socket → agent-api(server) → AgentApi
agentd   → agent-core::service → agent-core + agent-core::recovery
                        → runtime-api + DeploymentStore + JobRepository
         → runtime-nerdctl → runtime-api + app-model
         → agent-store-sqlite → agent-core + agent-api
agent-api → app-model + runtime-api + ingress-model（默认无 HTTP、SQLite、具体容器实现）
```

agentd 负责组装、管理员配置、信号和生命周期。agent-core::service 控制执行顺序，engine 实现一次部署，reconciler 实现一次恢复，SQLite 实现元数据/任务接口。agent-api 默认提供协议 DTO/服务端口，启用 feature 后提供传输和路由；客户端与服务端 feature 可分开启用，服务端接受 `Arc<dyn AgentApi>`。agentctl 不直接访问运行时或状态库。workspace 统一依赖和 lint，具体应用在 apps，可复用库在 crates。

## 并发与持久化

agentd 生命周期内持有 agentd.lock 和 SqliteStore 的 agent.lock，不允许第二个状态拥有者。API 和 worker 共享存储代理，互斥仅覆盖短元数据方法，不跨容器 await 持有。HTTP 使用当前线程 Tokio 执行器，阻塞 SQL 放入最多两个线程的阻塞池；独立 worker 线程以当前线程执行器串行处理全部变更和恢复。没有每应用线程、外部数据库或队列服务。

POST 先保存任务再返回；worker 按 ID 顺序 claim，执行并保存结果。单个任务失败不阻塞后续任务；节点级存储错误导致 worker 退出，agentd 退出交给 systemd 重启。启动将未完成 running 任务回 queued，快照启动队列并先处理，再恢复应用；持续新请求不会无限饿死恢复，每个任务边界检查是否该执行恢复轮次。一个慢运行时调用仍会延迟后续修改。

数据库 v3 保存配置/指纹/操作、DesiredState/RecoveryState、API 任务/幂等键。事务不跨运行时调用。v1/v2 自动迁移，旧程序拒绝新 schema。配置与任务 command 是私有明文；API apps/operations/job 不返回原始配置值。任务结果与错误使用独立协议 DTO，不暴露数据库行结构。SQLite 是元数据库，不保存业务应用数据。

## 生命周期与运行时边界

Apply：校验 → 保存 running 意图/Pending → 查询实际容器 → 必要时拉取/创建 → 核对归属和指纹 → 必要时启动 → 短暂观察 → Succeeded。错误记录 Failed，中断下次打开库标记 Interrupted，再核对实际状态。相同配置可重入；配置变更明确拒绝，尚无替换、更新或回滚。ID 是不可变容器 ID；实例名带长度编码，避免项目/名称分隔符碰撞。

stop/start 先保存 stopped/running 意图、清空退避，再协调。Stopped 不创建或启动，Running 才恢复。停止 daemon 不改变意图也不停止应用。退出/缺失按持久化退避恢复，稳定观察窗口后清零；只观察进程状态，不代表 HTTP 就绪。健康轮询不新增 apply 历史或反复写相同状态。

ContainerRuntime 是独立异步契约；nerdctl 适配负责参数、输出兼容、存在性查询、归属/配置标签和 ID 检查。超时/取消不证明 runtime 未执行；任何重试先核对实际状态。标签不是对 containerd 权限持有者的隔离边界。无强制删除、卷删除或全局 prune。数据卷/密钥通过独立 ResourceProvider 接口准备；网络、诊断与有界日志能力由运行时适配器提供；扩展仍应保持模型、流程与适配器的边界。

## 输入与权限边界

app-model 不依赖文件、进程或数据库。Identifier/ImageRef 构造及反序列化校验；AppSpec 拒绝未知字段，schema_version=1，fingerprint 对规范化配置计算 SHA256，不是镜像 digest 或密钥。应用请求不能设置 nerdctl 二进制、Socket、namespace 或命令超时。

运行命令使用固定绝对路径和参数数组，无应用 Shell 拼接；stdout/stderr 并发有界读取，超时覆盖退出和读流。Unix 独立进程组在异常/取消时尽力清理；成功退出保留可能继续工作的 runtime helper。已经交给 containerd 的工作不依赖 CLI 生命周期。

默认 network=none；显式 bridge 可最多 16 个 TCP 端口，host >=1024，固定 127.0.0.1。cap-drop ALL + no-new-privileges，不开放 host 网络、特权或任意挂载。共享 bridge 不构成项目隔离。Environment/args 只能保存非敏感配置，Debug/预览脱敏不能使明文存储/argv 变安全。SecretRef 与受控挂载已由 node-resources/ResourceProvider 实现，配置只引用名称。

HTTP 使用成熟解析器，版本化协议、正文/响应/连接/超时限制见 [API 文档](local-api.md)。Socket 只对可信本机管理员开放，访问权等价节点管理权限；本版管理 HTTP 保持回环监听；业务公网 HTTPS 使用独立 Caddy。

## Go 控制模块

根 go.work 包含 packages/go/agentapi 和 apps/controlplane。SDK 独立封装 v1 wire 类型、校验与 Unix HTTP；control 依赖 Agent 接口，httpapi 依赖 Manager 接口，cmd/controld 仅做配置/组装/信号。Go 不读写 Agent SQLite、不调用运行时、不另建执行队列。异步任务 ID 直接使用 Agent ID，幂等键由调用方提供并保存，控制端不在超时后换键重投。

HTTP 默认本机监听且要求私有 Bearer 令牌，最多 16 并发处理；已增加 Web 工作台，远程 Agent 传输和多用户授权仍未实现。部署指南见 [Go 控制端](controlplane.md)。未来扩展需维持单一节点写入者与端到端幂等语义。

## 后续扩展

未来 Go 保存账户、项目视图与审计，通过 API 提交意图，不共享 Agent 数据库；Go 停机不影响 Agent 维护应用。独立模式与托管模式应遵循同一写入协议。远程节点需独立的认证与安全传输设计。

第一版面向单节点、自用、可信工作负载；普通应用与外部模型 API 应用共用部署模型。2 GB 不承诺本地大模型推理或不可信多租户隔离。库可用 path/固定 Git revision 复用，暂不发布 crates.io。


## Web 与 TypeScript 模块

根 npm workspace 管理 apps/web 与 packages/ts/control-client。SDK 不依赖 React，组件按通用 UI、应用、任务组织；hook 管理轮询、浏览器会话恢复，App 负责组装。Token 只保存在页面内存；仅任务 ID 和非敏感未确认请求写入 sessionStorage。状态来源仍是 Agent，不把浏览器缓存视为权威状态。

Go webui 是独立静态适配器，公开登录页与构建资源；/api 前缀始终进入原认证路由。静态目录只包含管理员部署的构建产物，拒绝符号链接和目录列表。生产不运行 Node.js，不做 SSR；构建在开发/CI 执行。更详细的任务恢复语义见 [Web 指南](web.md)。

## 节点安装与发布

`distribution/release` 只在构建机组装 Linux 二进制、静态 Web 和固定运行时；`distribution/installer` 负责 Ubuntu 主机适配，不参与领域模型或部署任务执行。归档验证、网络选择、配置生成、安装记录/快照分别实现为小组件，shell 入口只负责检查、安装顺序和 systemd 编排。

一键安装独立管理 containerd 的 socket、镜像数据和 CNI bridge；运行时默认不启用 CRI 和构建服务。控制端仍通过 Unix Socket 连接 Agent，页面仍调用 Go HTTP API，Agent 不知道安装目录和安装包格式。版本选择、服务启停、文件恢复由安装器处理，应用的运行意图与容器恢复由 Agent 处理；两套状态各自存储，安装器不重写应用数据库。

详细路径和升级限制见 [安装指南](install.md)。

0.2.0 的 node-resources 通过 API 装饰器增加资源接口，密钥上传绕过任务队列，领域配置只记录名称；文件系统实现与 HTTP/SQLite/nerdctl 解耦。卷初始化只调整新空目录的属主，容器重建复用目录。资源保留、权限边界及数据库模板见 [有状态指南](stateful.md)。

0.3 在 runtime-api 增加可选日志/诊断端口，runtime-nerdctl 的 network/diagnostics 模块承担归属验证与受限探测。agent-core::service 在不参与部署写入排序的只读路径最多并发两个诊断调用；领域配置与任务仍只由原 worker 写入。Go/TypeScript SDK 同步合约，Web 诊断关闭后停止轮询。完整边界及四服务示例见 [完整部署指南](fullstack.md)。

当前通过 ingress-model、ingress 与其 caddy 模块 将域名策略、持久化协调和代理实现分离。Caddy 与安装器共享 ingress crate 提供的 bootstrap 模板；Go 证书检查只读取实际 TLS 元数据。参见 [域名与 HTTPS](ingress.md)。
