# 0.5.1 升级保障与业务验收

平台升级保留业务镜像、容器、数据卷、密钥、令牌、域名配置和停止意图。当前仍有短暂的控制端、Agent、Caddy 重启，不承诺公网请求和长连接零中断。运行时版本变化、不可向旧版本恢复的数据库迁移和平台降级需要单独的迁移方案。

## 升级顺序

1. 校验新包、旧 release 的完整文件清单和主机；检查新旧版本、架构、固定运行时/代理清单、候选二进制版本、API/状态路径版本、SQLite schema 和完整性，以及备份/预演所需磁盘空间。
2. 检查业务探针配置覆盖所有运行中的项目和域名；缺少配置在停止任何服务前拒绝升级。
3. 进入维护模式，拒绝新部署、启停、密钥写入、域名修改；等待已接受的 queued/running 任务完成，暂停新的容器恢复操作。默认最多等 300 秒，超时只解除门禁，继续原任务，不强杀任务。
4. 采集已有项目、容器 ID、启停意图、配置指纹、域名、密钥和卷元数据基线，执行只读业务探针。已停止且容器已删除的应用元数据是合法状态。
5. 停止应用层服务，再次检查没有未完成任务/操作和状态变化；一致性备份两个平台 SQLite，在私有临时副本上执行新 `agentd --check-state`。预演不连接运行时、不修改在线数据库。
6. 切换版本，新 Agent 根据持久化维护标记以禁止写入/暂停恢复的状态启动；域名协调仍可恢复代理配置。
7. 检查平台、原有容器身份、配置、意图、卷目录身份/属主/元数据、密钥/令牌摘要、域名同步，以及业务探针的 HTTP/HTTPS、JSON 断言和稳定数据值。默认等待 120 秒。
8. 全部通过才提交安装事务、清除维护标记、恢复新写入和后台恢复；私有备份目录保留 `upgrade-report.json` 与 `upgrade-baseline.json`；验证失败另记私有 `upgrade-failure.json`，迁移预演诊断保存在 `migration-rehearsal.stderr`。

维护门禁在 Rust `agent-api` 中实现，由 `agent-core`、`node-resources`、`ingress` 共享。许可跟随实际写入，包括不随 HTTP 超时取消的阻塞文件写入。排队任务、后台恢复和 API 写入都结束后才报告 quiescent。协议-only 使用不引入 Tokio。

0.4.1/0.5.0 没有维护 API：安装器暂时把旧 Unix Socket 移到私有升级入口，禁止新连接，等待已建立请求及持久化任务排空，停止后再检查未完成操作。失败则恢复入口；停止前失败不重启旧 Agent。未知的旧版布局拒绝猜测升级。旧版本没有新版本的 worker 静默状态保证，因此停止后的二次检查不可省略。

## 配置业务探针

平台无法自动知道你的后端连接了哪些数据库、哪条记录代表重要数据。每个运行中的项目至少提供一个**无副作用的 GET 检查接口**；每个运行应用的已启用域名还需要 HTTPS 探针。检查接口应实际读数据库/Redis，不能只返回固定字符串。

在服务器创建 `/etc/deploy-platform/upgrade-checks.json`，权限设为 `0600`：

```json
{
  "schema_version": 1,
  "probes": [
    {
      "project": "fullstack",
      "url": "https://app.your-domain.com/api/upgrade-check",
      "status": 200,
      "json_equals": { "service": "backend" },
      "unchanged_json": ["postgres", "redis"]
    }
  ]
}
```

上述接口通过前端代理连接后端，后端只读 PostgreSQL 与 Redis，`unchanged_json` 对比升级前后的顶层字段。四服务示例镜像已增加这个接口；需先按 [四服务指南](fullstack.md) 在开发机构建新示例镜像并部署。它要求示例中已有 visits 记录和缓存值，首次部署后先通过页面写入一条记录。旧示例镜像没有此接口。

真实业务应选择稳定的验证记录/标记；正在变化的订单总数、访问计数等不适合用作 unchanged_json，否则正常流量也可能使验收失败。`json_equals` 用于检查依赖状态等固定语义。不要在探针配置或响应中放密码、API Key、个人数据等敏感值。

无域名时，可检查该项目已发布的回环端口，例如 `http://127.0.0.1:18080/api/upgrade-check`。有域名时 HTTPS 实际连接本机 443，同时验证原域名的 SNI、有效期和信任链，避免公网 DNS/代理绕过本机实例。默认使用系统 CA；隔离测试可显式配置 `ca_file`，没有跳过 TLS 校验的开关。

配置不接受 Shell、任意外部地址、管理端口、URL 凭据、查询参数或跳转。至多 32 个探针，串行执行，单次请求超时 5 秒、响应上限 64 KiB；不会在 2 GiB 节点另拉镜像或创建测试数据库。没有运行项目时可不配置探针；已全部停止的已知项目会跳过其现有探针，停止意图保持不变。仅有数据库的项目也需要一个受控只读验证入口，否则本版自动升级会拒绝，需规划人工维护流程。

## 执行与排错

```bash
sudo chmod 600 /etc/deploy-platform/upgrade-checks.json
bash deploy-platform-0.5.1-linux-amd64.run --check
sudo bash deploy-platform-0.5.1-linux-amd64.run --upgrade \
  --drain-seconds 300 --verify-seconds 120
sudo cloudctl maintenance status
sudo cloudctl status
```

ARM64 使用 arm64 包。可通过 `--checks-file /绝对路径/配置.json` 指定私有配置，没有跳过业务验证的升级参数。

维护状态也能用 `cloudctl maintenance enter|status|exit` 或独立 `agentctl` 查询/操作；只通过节点私有 Socket 提供，不暴露给 Go/Web 管理 API。安装事务尚在进行或中断时，不要手动 exit；重跑同一个安装包，让事务恢复逻辑处理。若事务记录已删除、安装验收已通过，但解除门禁请求超时，可确认 `maintenance status` 后手动 exit。

停止服务前失败：恢复旧入口并解除门禁，不取消已有任务。切换后失败：在数据库仍可被旧版本读取时恢复旧程序/配置并恢复原活动服务；恢复本身失败会保留事务记录，不能误报安装成功。若在线 schema 已不兼容，阻止旧程序启动，保留数据库与备份，需人工制定恢复方案；不会自动用旧数据库覆盖新数据。

Agent SQLite 的所有迁移步骤、运行时目标校验和中断状态修复放在同一事务中，任一步失败全部回滚。发布兼容性清单在 [compatibility.json](../distribution/release/compatibility.json)，打包时嵌入 manifest，不能只凭版本号断言安全。

卷目录与元数据保持不变，只读业务探针验证选定的数据；**它们不能证明整个业务数据库逐字节一致，也不构成业务备份**。仍需独立验证 PostgreSQL/Redis、应用文件及证书存储的备份恢复。

## 测试入口

`make test` 包含维护排空/拒绝写入/恢复、真实 Go→Rust 门禁、迁移失败原子性，以及 Python 兼容性、磁盘、超时、旧版排空、TLS、探针和回滚故障注入测试。

真实 Linux/containerd 验收在可删除隔离节点运行：

```bash
bash distribution/tests/upgrade-smoke.sh \
  /绝对路径/deploy-platform-0.5.0-linux-arm64.run \
  /绝对路径/deploy-platform-0.5.1-linux-arm64.run \
  /绝对路径/upgrade-demo.tar
```

测试镜像归档必须包含对应架构的 `cloud-fullstack-demo:upgrade-check`，使用当前四服务示例构建。脚本限制节点为 2 CPU/2 GiB，验证旧版升级、本地 CA 的真实 HTTPS、已有数据库/缓存记录、验证失败后的恢复和新版维护 API 重装。CA 只在该可删除节点内受信，不修改开发机信任库。
