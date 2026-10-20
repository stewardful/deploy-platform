# 持久化数据卷与密钥

本文适用于 0.5.0，介绍自 0.2.0 引入的数据卷与密钥能力。网页可以部署 PostgreSQL、Redis 等需要本机数据目录的应用，沿用 Go 控制端 → 私有 Unix Socket → Rust Agent → nerdctl/containerd。没有增加数据库管理进程、消息队列或常驻密钥服务。

## 网页使用

1. 从“数据与密钥”选择项目，保存密钥。PostgreSQL 使用名称 `db-password`、单行密码；Redis 选择“Redis 密码”类型，保存为 `redis-config`，页面自动生成包含 `requirepass` 的配置文件。
2. 回到“应用管理”→“部署应用”，载入 PostgreSQL 或 Redis 模板，填写同一个项目名。模板只包含密钥名称和文件路径，不包含真实密码。
3. 检查 UID/GID、数据卷路径、端口和资源限制后提交。模板使用官方 Debian 镜像的数字用户 `999:999`，保留全部 capability 禁用和 no-new-privileges，不为了数据库开启 privileged。
4. 等待应用初始化。当前网页模板使用 project 网络且默认不映射宿主端口；同项目后端连接 `postgres:5432`（用户 `postgres`、数据库 `app`）或 `redis:6379`，使用刚保存的密码。需要电脑上的数据库客户端访问时，部署前显式增加回环端口映射，例如 PostgreSQL 的 `15432 → 5432` 或 Redis 的 `16379 → 6379`，再建立 SSH 隧道。
5. “数据与密钥”输入项目与应用名可查询卷名称、容器路径和 UID/GID。这里只查询元数据，不提供删除数据按钮。

PostgreSQL 模板：`postgres:17-bookworm`、384 MiB、0.5 CPU、128 PID、shared_buffers 64 MB、max_connections 30。Redis 模板：`redis:7.4-bookworm`、256 MiB、0.5 CPU、128 PID、maxmemory 128 MB、AOF 每秒同步、noeviction。镜像版本升级需要单独的数据迁移，不能盲目更换 PostgreSQL 主版本。生产建议固定经过验证的镜像 digest。

2 GiB 节点应按实际内存、镜像解压和磁盘余量逐步增加项目。模板不是性能容量承诺；Redis maxmemory 不包括全部进程开销，PostgreSQL 每连接也会增加内存。初始化成功和数据保留不等同于高可用或业务备份。

## 配置模型

```json
{
  "schema_version": 1,
  "app": { "project": "demo", "name": "db" },
  "image": "docker.io/library/postgres:17-bookworm",
  "resources": { "memory_mib": 384, "cpu_millis": 500, "pids": 128 },
  "user": { "uid": 999, "gid": 999 },
  "network": "bridge",
  "ports": [{ "host": 15432, "container": 5432 }],
  "volumes": [{ "name": "data", "target": "/var/lib/postgresql/data", "read_only": false }],
  "secrets": [{ "name": "db-password" }],
  "environment": {
    "POSTGRES_PASSWORD_FILE": "/run/secrets/db-password",
    "POSTGRES_DB": "app",
    "PGDATA": "/var/lib/postgresql/data/pgdata"
  },
  "args": ["postgres", "-c", "shared_buffers=64MB", "-c", "max_connections=30"]
}
```

最多 8 个数据卷和 16 个密钥引用。卷路径只允许规范的容器目录 `/data`、`/var/lib`、`/srv`、`/opt`、`/app`、`/workspace` 及其子目录，不接受宿主机路径、相对路径、路径跳转或重叠挂载。密钥目标固定为 `/run/secrets/<名称>`，始终只读。

`user` 是可选数字 UID/GID；不填写时保留镜像用户，卷初始属主默认 0:0，因此非 root 镜像应明确填写匹配的 UID/GID。更换镜像发行版可能改变用户编号，需先确认。只在新建空卷时设置属主，不递归 chown 已有数据库文件、不修改现存数据权限。

新字段为空时不参与序列化，保留 0.1.0 应用的指纹、任务幂等和容器标签。卷、用户、密钥引用有变化会改变指纹；目前仍拒绝原地修改已有应用配置。

## 持久化与生命周期

数据卷是 **Agent 托管的目录 bind mount**，不是任意宿主路径或 Docker/nerdctl 随机匿名卷：

```text
/var/lib/deploy-platform/resources/
  volumes/<AppKey 的防碰撞实例名>/<卷名称>/    应用数据
  volumes/<实例名>/<卷名称>.json             属主与容器路径元数据
  secrets/<项目>/<密钥名>/value              私有密钥文件
```

卷按项目 + 应用 + 逻辑名称隔离。停止应用、删除/重建容器、重启 Agent 或主机都不删除卷；缺失容器会按原配置挂载同一目录。没有跨应用共享、自动收养已有目录、删除卷、容量配额、远程存储和自动备份接口。磁盘满会导致数据库失败，应由管理员监控。

容器删除不会执行 `--volumes`，恢复服务也不调用 prune。应用之外的进程不要修改卷元数据；不匹配的 UID/GID、未登记目录或符号链接会被拒绝。

## 密钥边界

密钥通过独立写入 API 保存，**不会进入 AppSpec、SQLite 配置/任务、普通查询、运行时命令行或容器初始环境变量**。网页不把内容保存进 localStorage/sessionStorage，也不调用通用任务草稿存储；提交时清空输入。服务只返回项目和名称，列表不返回原文、摘要或字节数。

这一版是**本机权限保护的明文文件存储，没有静态加密、KMS、Vault 或多用户授权**。宿主机目录由 Agent 用户持有并限制为 0700；文件为 0444，以便绑定进容器后匹配任意非 root 镜像用户，并通过只读 mount 禁止容器修改。外层私有目录使其他宿主普通用户无法遍历。默认服务是 root，主机 root、获得管理 token 的可信管理员和挂载该密钥的应用都能读取内容。应用自己若打印密钥，平台无法阻止它写进应用日志。

每份内容限制 1..65536 UTF-8 字节、不能含 NUL。一个名称对应不可变内容：相同项目、名称、内容重试成功；同名不同内容返回 conflict。没有读取、覆盖、删除或热轮换 API。连接中断后可重新输入相同内容确认；不要把真实内容写进普通环境变量或启动参数。

PostgreSQL 使用官方入口的 `POSTGRES_PASSWORD_FILE`；初始化完成后实际密码存储在数据库内部，不能通过换配置文件自动改密码。Redis 读取受保护的配置文件，密码不放在 `--requirepass` 命令参数中。[PostgreSQL 上游入口](https://github.com/docker-library/postgres/blob/master/17/bookworm/docker-entrypoint.sh)、[Redis 上游入口](https://github.com/redis/docker-library-redis/blob/master/7.4/debian/docker-entrypoint.sh)。

## 独立 Agent 与 API

```bash
# 内容从管理员私有文件读取，不要把密码放进命令行参数
sudo cloudctl secret create --project demo --name db-password --file /安全目录/password.txt
sudo cloudctl secret list --project demo
sudo cloudctl volume list --project demo --name db
sudo cloudctl deploy -f /opt/deploy-platform/current/examples/postgres.toml
```

独立 Agent 使用 `agentctl --socket /run/deploy-platform/agent.sock ...`，不需要 Go 控制端。密钥输入文件要主动清理或安全保存；不要将它提交进仓库。

| Agent Unix API | Go HTTP API | 行为 |
| --- | --- | --- |
| POST `/v1/secrets` | POST `/api/v1/secrets` | JSON `{project,name,value}`；202，仅返回名称 |
| GET `/v1/secrets/{project}` | GET `/api/v1/secrets/{project}` | 名称列表 |
| GET `/v1/volumes/{project}/{name}` | GET `/api/v1/volumes/{project}/{name}` | 指定应用卷元数据 |

Go HTTP 入口继续要求 Bearer token，默认回环地址 + SSH 隧道；Unix API 继续依赖私有 Socket 权限。密钥上传不创建持久化 job；应用部署 job 只引用名称。具体接口见 contracts 中的 OpenAPI。

## 模块划分

- `app-model`：可移植的用户、卷、密钥引用与路径校验，零文件系统依赖。
- `runtime-api::ResourceProvider`：准备宿主资源的可选接口，只返回路径和挂载元数据。
- `node-resources`：私有文件、不可变写入、卷初始化/检查和 API 装饰器；不依赖 SQLite、HTTP 或 nerdctl。
- `runtime-nerdctl`：通过可选 provider 组装 bind mount、只读标记和数字用户，不解析密钥内容。
- `agentd`：组装资源组件与现有服务。其他运行时可以复用同一 provider，其他存储实现可以替换该宿主适配器。
- Go/TypeScript SDK、Web 的 MountEditor、ResourcesPanel 和无敏感值模板分别独立复用。

## 升级、备份和自检

已有安装按 [安装指南](install.md) 用当前版本安装包和 `--upgrade` 升级。**开始部署带数据卷与密钥字段的应用后，不应降级到不认识它们的 0.1.0 Agent。**现有安装器只备份平台 SQLite 和安装配置，不复制数据库数据目录或密钥；这不构成数据库备份。

数据库使用逻辑备份或一致性快照；直接复制运行中的 PostgreSQL 数据目录不保证可恢复。密钥文件和卷需要单独的、受保护的备份与恢复方案。不要将明文密钥备份上传到公共存储。恢复平台数据时同时恢复资源目录、属主和密钥名称，避免应用恢复却缺少其数据。

可在空闲测试节点执行：

```bash
sudo cloudctl test stateful
```

该自检串行部署真实 PostgreSQL、Redis，认证后写入数据，停止 Agent 并删除各自测试容器，再启动 Agent 自动重建并读回原数据，检查普通 API/inspect 没有上传的密钥值。最后保存停止意图并移除自己的测试容器；**测试数据目录、密钥和镜像保留**，不自动删除任何卷。它会短暂中断 Agent 管理操作，不应在繁忙业务期间执行。

自检覆盖容器丢失/Agent 重启，不替代真实机器断电、磁盘故障、长时压力和业务恢复演练。平台仍不提供数据库集群、复制、高可用、自动密码轮换和跨项目服务发现。

开发机完整安装验收可执行 `STATEFUL=1 bash distribution/tests/ubuntu-smoke.sh "$PWD/target/dist/deploy-platform-0.5.0-linux-arm64.run"`，脚本清理专用测试节点，不挂载宿主目录。
