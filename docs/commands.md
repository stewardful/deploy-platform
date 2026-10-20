# cloudctl 命令

`cloudctl` 是安装节点的统一入口。应用操作复用独立 Rust Agent；平台更新复用已有更新 worker 和安装器，不自行读写应用数据库。本版统一命名，不保留旧 cloudctl 命令别名。

## 日常应用管理

```bash
sudo cloudctl help
sudo cloudctl help deploy
sudo cloudctl apps
sudo cloudctl validate -f app.toml
sudo cloudctl preview -f app.toml
sudo cloudctl deploy -f app.json
sudo cloudctl start --project demo --name web
sudo cloudctl stop --project demo --name web
sudo cloudctl logs --project demo --name web --tail 50
sudo cloudctl diagnostics --project demo --name web
sudo cloudctl operations
sudo cloudctl job 1
```

`preview` 取代旧 `plan`，只做离线校验并输出配置摘要；不读取服务器现状、不比较变更、不预演容器部署。环境变量只展示键名，密钥只展示引用。`validate` 只校验。二者都不要求 Agent 运行。

`deploy` 默认等待持久化任务终态；`--no-wait` 只返回已保存任务，随后用 `job` 查询。失败返回非零退出码；202 不代表部署成功。JSON/TOML 必须是普通 AppSpec，不能传 HTTP 请求信封。同名应用配置变化目前会被拒绝；此命令尚不提供业务应用镜像替换发布。

## 数据与域名

```bash
sudo cloudctl secret create --project demo --name api-key --file /私有路径/key.txt
sudo cloudctl secret list --project demo
sudo cloudctl volume list --project demo --name web
sudo cloudctl route list
sudo cloudctl route set --file route.json --expected-revision 0
sudo cloudctl route delete --domain app.your-domain.com --expected-revision 1
```

密钥从文件读取，不放在命令行参数中；列表只返回元数据。数据卷仍由部署配置创建，`volume list` 只查询。域名编辑/删除使用实际查询到的版本，详见 [域名指南](ingress.md)。没有新增删除应用数据或全局清理命令。

## 状态、版本与日志

| 命令 | 实际范围 |
| --- | --- |
| `status` | 全节点：运行时、Agent、Go/Web、代理及认证；要求控制端运行 |
| `agent-health` | 只检查 Agent API 版本及是否接受任务，不表示容器或业务健康 |
| `diagnostics --project P --name APP` | 应用运行状态和按需 HTTP 就绪检查 |
| `logs --project P --name APP` | 应用 stdout/stderr，默认 100 行，有输出限制 |
| `system-logs SERVICE` | 指定 agent/control/runtime/proxy/update 服务最近 100 行日志 |
| `version` / `--version` | JSON：平台版本、架构、包类型、构建来源及组件版本；不访问网络 |

`logs` 不再默认读取 Agent 系统日志；必须明确指定应用。系统日志必须显式指定服务。`version` 中 Agent 版本来自二进制，控制端版本来自已安装包 manifest，运行时/代理版本来自安装锁定清单；它不是业务容器镜像清单。旧包没有构建来源时 `build` 为 null。

## 正式升级与 dev 更新

正式版使用已经存在的管理 API：

```bash
sudo cloudctl update check
sudo cloudctl upgrade --version 0.5.4 --yes
sudo cloudctl update status
sudo cloudctl system-logs update
```

先检查，审核返回的候选版本，再显式提交准确版本。`upgrade` 只接受最近检查过的正式 Release，不安装 dev、alpha/beta 或任意 URL。需要控制端运行且配置更新功能；没有正式 Release、凭据无效或缺少产物会失败，不会假装升级成功。任务提交成功只表示排队。

dev 使用独立命令，默认选择最新通过构建和测试、产物可用且架构匹配的 dev 构建，也可以指定 GitHub Actions 运行编号：

```bash
# 查看最新可用 dev 构建，不下载、不排队
sudo cloudctl upgrade-dev
# 选择并提交当前最新可用构建
sudo cloudctl upgrade-dev --yes
# 指定构建；也适用于审核后锁定预览中返回的运行编号
sudo cloudctl upgrade-dev --run-id 123456789
sudo cloudctl upgrade-dev --run-id 123456789 --yes
sudo cloudctl update status
```

`upgrade-dev` 最多搜索最近 100 个已结束的 dev 工作流，按构建序号而非重跑完成时间排序。无可用候选时失败；指定 `--run-id` 时不搜索、不回退到其他构建。鉴权、限流、网络或响应损坏会中止搜索。无编号的预览与 `--yes` 是两次独立查询；要批准与预览完全相同的构建，请把返回的 `run_id` 传给 `--run-id`。任务入队后已固定运行编号、提交及产物身份，执行器不会再次选择“最新”。

`upgrade-dev` 直接读取已配置仓库的 GitHub Actions 元数据，不依赖 Go 控制端，也不需要服务器另装 deploy-delivery。它复用平台更新 path/service，按需执行，无新增常驻服务或轮询。它只更新平台，不更新业务应用镜像。当前没有独立 alpha/beta 发布通道，不用这些名字代指 dev。

私有仓库手动 dev 下载需要服务器凭据对该仓库有 **Actions: read**；正式 Release 读取需要 **Contents: read**。共用 `/etc/deploy-platform/updates.json` 的仓库和私有 token 文件配置，详见 [更新指南](updates.md)。CI 自带临时 GitHub token 不会自动提供给服务器，不能仅凭仓库 URL 下载私有产物。

源码、测试、架构、摘要和 dev 构建来源会在执行前重新核对；产物变化要求重新检查。dev 安装允许同版本不同提交；不自动放开数据库兼容限制。正式升级允许同版本 dev 构建切回正式 Release，仍拒绝更旧版本。`update status` 直接读取私有任务记录，在控制端重启时仍可使用；若执行器中断，会标记 interrupted，不自动重试。

两条入口共用更新请求锁、worker 和原安装器的兼容检查、排空、备份、迁移保护、验证和失败回退。原安装包 `--upgrade` 继续作为离线升级和恢复入口，详见 [升级保障](upgrade.md)。

## 高级操作

- `token`：以 root 输出 Web 管理令牌；不是登录或密钥轮换。
- `maintenance status|enter|exit`：升级写入门禁；不是升级命令，升级期间不要手动解除。
- `test smoke|stateful|fullstack`：真实验收测试，会创建资源，部分会重启 Agent；请在测试节点使用。
- `runtime ...`：直接访问 nerdctl，会绕过 Agent 的期望状态管理，仅用于管理员排错。
- `agentctl ...`：独立 Agent CLI 的显式转发入口。

旧 `apply/list/verify/doctor/plan` 及平铺的 `secrets/volumes/routes/secret-create/route-set/route-delete`、旧测试命令和 `logs agent` 已从 cloudctl 移除。独立 `agentctl` 的命令协议不变：例如 `agentctl apply`、`agentctl plan` 仍供其他项目复用。

独立 Agent 安装不需要 cloudctl、Go 或 Web。日常 cloudctl 应用命令只依赖 agentd 的 Unix Socket 和单写入者任务流程；部署方式见 [独立 Agent](agent.md)、[一键安装](install.md)。
