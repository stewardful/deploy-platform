# 发现新版本与网页升级

适用 0.5.2 起的完整安装包。此功能更新 deploy-platform 自身，不更新业务镜像。检查版本不下载、不安装、不停止项目；只有管理员在网页确认后才执行升级。

## 发布版本

日常推送默认分支 `dev` 自动更新测试机；正式代码推送 `main` 后，由 [发布工作流](../.github/workflows/release.yml) 自动测试、构建两种架构、创建固定到 main 提交的版本标签并发布 GitHub Release。无需手动推送标签。完整 Secrets 配置及分支约定见 [CI/CD](ci-cd.md)。

每次正式发布需提升源码版本，Rust workspace、内部组件和 Cargo.lock 保持一致。同一版本不能指向其他提交；同一提交重跑可继续草稿，已经公开的资产不会覆盖。手动触发发布也必须选择 main 分支。

私有仓库的 Release 仍然是私有的，不会因为发布流程而公开源码。首次采用此功能需要先手动安装或升级到含更新器的版本；旧版无法凭空出现网页升级按钮。

## 服务器配置

默认检查 `stewardful/deploy-platform` 的最新正式 GitHub Release，每小时一次，控制端启动约十秒后首次检查。手动检查至少间隔一分钟，包括失败请求，防止耗尽 GitHub 配额。页面每三十秒查询本地状态，有升级任务时每五秒查询；隐藏标签页暂停查询。

公共仓库无需凭据。私有仓库需要管理员创建仅允许该仓库 **Contents: read** 的 GitHub fine-grained token，写入服务器 `/etc/deploy-platform/github.token`，权限为 `0600`，不要放在源码、Shell 历史或网页中。再创建：

```json
{
  "repository": "stewardful/deploy-platform",
  "token_file": "/etc/deploy-platform/github.token"
}
```

保存为 `/etc/deploy-platform/updates.json`，同样 `root:root`、`0600`。仓库和 token 文件配置会在检查/执行时重新读取。管理员配置文件不被安装包覆盖。凭据轮换后最长等一分钟再检查；页面不会返回凭据、下载签名 URL 或原始上游错误。

服务器需能通过 HTTPS 访问 `api.github.com`、`github.com` 以及 GitHub 的资产存储域名。下载器只允许明确列出的 GitHub 域名，跨域跳转移除 Authorization，不接受自定义下载 URL、Shell、安装参数或跳过 TLS 校验。查询和资产下载使用 [GitHub Release API](https://docs.github.com/en/rest/releases/assets?apiVersion=2022-11-28)。

## 网页操作

1. 打开工作台，顶部显示当前版本；自动检查发现更新后显示新版本，也可以点击“检查更新”。
2. 按 [安全升级指南](upgrade.md) 配置 `/etc/deploy-platform/upgrade-checks.json`。运行项目缺少只读业务探针时，原安装器会拒绝升级。
3. 点击“升级到 X.Y.Z”，在弹窗确认维护时间和影响。
4. 页面显示排队、确认版本、下载、包检查、安装和最终结果。下载流式写盘，上限 512 MiB，总下载期限约十五分钟，不把整个包读入内存。
5. 下载前重新核对已确认的版本、架构、资产 ID、大小和 GitHub SHA256；任何资产变化都要求重新检查版本。完整下载且摘要一致后才运行包自检，再调用原有 `--upgrade --skip-apt` 安装入口。
6. 下载包会自动删除；原安装器的 release 目录、备份和事务记录按原策略保留。成功后刷新页面加载新 Web；失败后查看日志，确认原服务恢复情况后再明确重试。

GitHub API 的 SHA256 和 TLS 防止传输损坏、下载错包及确认后资产变更；它们不是独立发布者签名，信任边界仍然包括仓库维护者及 GitHub 账号权限。

## 执行边界与恢复

Go 的 `updates` 组件只检查版本、缓存状态和原子提交私有请求。`deploy-update.path` 监视请求文件，按需启动 `deploy-update.service`，服务内 Python 标准库执行器流式下载后调用既有安装器。没有新增常驻运行时或数据库，也没有新增 Agent crate；独立 Rust Agent 不依赖此功能。

更新服务拥有自己的 systemd cgroup，不属于控制端。安装器停止并重启 Web/Go、Agent、Caddy 时，下载/安装任务继续。containerd 和业务容器不会被主动停止，但 Caddy 重启会使公网入口短暂中断、长连接断开。兼容检查、任务排空、数据库备份及迁移预演、项目数据/HTTPS 验证和兼容失败回退均复用原流程。

状态保存在 `/var/lib/deploy-platform/updates`（0700）：`request.json` 排队，`active.json` 执行中，`result.json` 最终/进度结果。任一请求或执行记录存在时拒绝重复升级。安装器自身节点锁同时防止命令行与网页并行激活。

```bash
sudo systemctl status deploy-update.path deploy-update.service
sudo journalctl -u deploy-update.service -n 100 --no-pager
sudo cat /var/lib/deploy-platform/updates/result.json
```

失败不等于“已经回退成功”，请结合日志、原安装器的事务记录和业务访问确认。若服务器重启、执行器被杀导致 `active.json` 残留，网页显示“interrupted”，不会自动重试或清除事务。先确认更新服务已经停止，按 [安全升级恢复步骤](upgrade.md) 重跑可信安装包恢复事务并验证业务，再由管理员归档残留 `active.json` 后重新检查。不得仅删除安装事务或强制启动旧 Agent。下载中断残留的 `/var/tmp/cloud-update-*` 私有临时目录也只应在确认执行器未运行后清理。

已接受的 `request.json` 在服务重启后仍会由 path 单元执行，因为它代表已经明确确认的升级；已进入 `active.json` 的中断操作不会重放。

## API 与验证

所有接口沿用管理 Bearer Token，默认只监听回环地址：

- `GET /api/v1/updates`：当前版本、候选版本、检查时间/错误及执行状态。
- `POST /api/v1/updates/check`，JSON `{}`：检查版本，返回 202 与状态；失败信息位于状态 `error`。
- `POST /api/v1/updates/upgrade`，JSON `{"version":"0.5.3"}`：只能提交最近一小时内检查过的更新版本；202 表示已排队，不代表成功；版本变化或已有任务返回 409。

配置不启用更新目录的开发控制端返回 `enabled:false`，网页隐藏更新区。测试覆盖可信元数据、私有凭据、重复点击、陈旧候选、重定向脱敏、下载大小/摘要/磁盘限制、执行中断和独立安装入口。Web 浏览器测试使用隔离 API 响应验证提示、确认与刷新不重复提交；这不替代真实 GitHub Release 和腾讯云验收。

## 命令行更新与 dev 构建

命令行正式升级复用上述 API 和 worker：`cloudctl update check` 后显式运行 `cloudctl upgrade --version X.Y.Z --yes`。查询执行结果用 `cloudctl update status`，该查询不依赖正在重启的控制端。应用操作、平台升级和业务应用发布的边界见 [命令指南](commands.md)。

`cloudctl upgrade-dev` 默认查找最新通过构建和测试且产物可用的 dev 构建，只展示固定元数据；`cloudctl upgrade-dev --yes` 选择并提交当前最新候选。保留 `--run-id RUN_ID` 指定某次构建；指定模式失败时不会自动选择其他构建。私有仓库需要服务器配置的 token 对该仓库有 **Actions: read** 权限；正式版 Contents 只读权限不足以下载 Actions 产物。凭据仍从现有私有文件读取，不能传在命令行、日志或网页参数中。

默认查询最多最近 100 个已结束运行，按工作流构建序号从新到旧筛选；旧构建重跑不会因此成为最新。跳过不合格来源、测试失败、产物缺失/过期/架构不符的候选；鉴权失败、限流、网络或元数据响应损坏时直接中止，不把 API 故障当作无效候选。没有可用构建时失败，可用 `--run-id` 查询搜索范围以外的构建。无编号预览和确认是独立查询，需要与预览完全一致时显式传回运行编号；入队后始终固定该构建，执行时只重新核对它，不重新选择最新版本。

dev 来源固定为配置仓库的 `.github/workflows/dev-deploy.yml`、`dev` 分支、push/workflow_dispatch，拒绝 PR、fork 和其他工作流。运行必须结束，且 `build / test` 与对应架构的 package 任务成功；若只是最后 SSH 部署失败，仍可手动拉取已验证的构建。必须存在唯一、未过期、架构匹配的 `deploy-platform-linux-ARCH` 产物，包含 `.run` 与 `.sha256`。GitHub 产物保留期目前三天，过期后需重新构建，不能靠运行编号恢复过期文件。初版按一次至多 100 条任务/产物检查，超出造成目标缺失时拒绝安装。

worker 在下载前再次核对运行编号、提交、产物 ID、大小和 GitHub SHA256，使用已有流式下载器和固定版本 ZIP 提取组件。下载后核对包 manifest 的 dev 来源与提交 SHA；旧包缺少来源记录时拒绝安装，需要由新打包流程重新构建 dev。没有 alpha/beta Release 自动选择，也不支持任意下载 URL。

dev 不增加 API、常驻服务或版本轮询；CLI 原子提交同一个更新队列，执行器继续调用原安装器 `--check`、`--upgrade --skip-apt`。Web 和 CLI 的提交由跨进程锁串行化，安装器节点锁同时覆盖离线包和独立 CI 工具。更新结果包含 channel/run_id/commit，失败不会自动重试。

首次安装与后续升级统一为 cloudctl。首次执行 `cloudctl install --channel dev|stable` 或 `--file`，之后使用 upgrade/upgrade-dev；全部共用下载组件、受保护安装器和任务状态。CI 同样使用平台自带的 cloudctl delivery，不依赖外部工具仓库。详见 [统一首次安装](install.md#统一-cloudctl-首次安装)。
