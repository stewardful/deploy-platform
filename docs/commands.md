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

`logs` 不再默认读取 Agent 系统日志；必须明确指定应用。系统日志必须显式指定服务。`version` 中 Agent 版本来自二进制，控制端版本来自已安装包 manifest，运行时/代理版本来自安装锁定清单；它不是业务容器镜像清单。未安装平台时返回 installed=false 和 CLI 自身版本。

## 正式升级与 dev 更新

正式版通过独立 CLI 检查并明确确认安装：

```bash
sudo cloudctl update check
sudo cloudctl install --channel stable --version 0.5.4 --yes
sudo cloudctl update status
sudo cloudctl system-logs update
```

先检查，审核返回的候选版本，再用 --version 和 --yes 明确确认。CLI 不依赖控制端；网页仍使用已有管理 API。没有正式 Release、凭据无效或缺少产物会失败。

dev 默认选择最新通过 CI 并公开发布的构建，也可以指定已发布的构建编号：

```bash
# 确保最新公开 dev 构建已安装；默认等待任务结束
sudo cloudctl install --channel dev
# 固定构建，提交后通过 update status 查询
sudo cloudctl install --channel dev --run-id 123456789 --no-wait
sudo cloudctl update status
```

`cloudctl install --channel dev` 发现最新公开构建并固定清单；指定 --run-id 则只查询固定构建。--commit 绑定预期完整 SHA。已有同构建返回 unchanged，新构建复用保护升级；正式或本地更新要求 --yes。--automatic 只供固定 dev 构建，并要求本地开关授权。

它不依赖 Go 控制端，复用平台更新 path/service，按需执行，无新增常驻服务或轮询。只更新平台，不更新业务应用镜像。正式 Release 私有下载可沿用 `/etc/deploy-platform/updates.json` 中的 Contents:read token 配置，公开 dev 路径不读取该 token。详见 [更新指南](updates.md)。

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


首次安装默认使用 [一行安装入口](install.md#统一-cloudctl-首次安装)，内部复用 `cloudctl install --channel dev|stable` 或 `cloudctl install --file INSTALLER.run`，具体获取命令、凭据和成功信号见 [统一首次安装](install.md#统一-cloudctl-首次安装)。首次安装与后续升级使用同一 CLI；`install` 对同构建跳过，对新构建执行保护升级。本地通知策略用 `cloudctl update policy` 管理；Webhook 只触发固定安装命令，平台不接收通知。

候选 dev 构建必须通过 cloudctl 构建、完整测试及对应架构打包；缺少其中任何任务均不接受。

cloudctl 由本 monorepo 独立打包，`setup` 注册到 `/usr/local/bin/cloudctl`；安装缓存为 `/opt/cloudctl/releases`。`install --automatic` 只允许固定 dev run+commit，并由本地开关授权。通知服务见 [deploy-webhook](https://github.com/stewardful/deploy-webhook)，平台没有 Webhook API。旧 upgrade/upgrade-dev 命令已移除，无别名。
