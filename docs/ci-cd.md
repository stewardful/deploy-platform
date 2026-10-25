# CI、发布与本机通知升级

| 触发 | 行为 | 是否部署服务器 |
| --- | --- | --- |
| PR | 检查、构建和隔离 Ubuntu 安装/升级验证 | 否 |
| dev 推送 | 完整测试、双架构更新包、公开 dev 预发布 | 否 |
| main 推送 | 双架构完整离线包与正式 Release | 否 |

Actions 不持有服务器登录权限，不发送远程命令，也不调用服务器管理 API。没有 SSH 部署任务、主机/用户/端口/私钥/known_hosts 配置、TEST_SERVER_AUTO_DEPLOY 开关、独立 CI 交付 worker 或旧 cloudctl delivery 命令。仅接受当前包、状态和通知契约，不保留旧交付兼容路径。

## 零自定义 Actions 配置

公开构建发布使用 GitHub 自动提供的 GITHUB_TOKEN，publish job 显式声明 Contents:write 和 Actions:read；不需要自定义 Secrets、Repository variables、PAT、GitHub App 或跨仓库 CI 触发。推送 dev 的源码经过测试和 cloudctl/amd64/arm64 打包后才发布。仓库默认分支是 dev。

[dev 发布工作流](../.github/workflows/dev-release.yml) 先将完整资产上传固定 `dev-RUN_ID` 草稿，再公开预发布，固定标签指向 dev 事件 SHA。已公开资产不可由流程覆盖，重跑冲突需新构建。`dev-latest` 提供最新发现清单、cloudctl 和一行安装入口 install.sh，安装包始终从固定标签下载。旧源码或旧构建重跑不回退最新指针，预发布不成为正式 latest。

固定构建包含两架构平台更新包、摘要、cloudctl、install.sh 及校验、dev.json；dev 包不重复打入基础运行时，按版本下载或复用。Release 附件不受 Actions 三天缓存期限影响，暂无自动清理；被删除的构建不能按编号恢复。正式版本仍保留完整离线包，并发布同一 install.sh 入口。CLI job 渲染脚本时固定来源标签和 CLI 摘要，发布前再次逐字节核对脚本、CLI 与校验文件；不接受缺失入口或其他构建生成的入口，不补充旧发布资产。

main 发版前提高 workspace 和内部组件版本，更新 Cargo.lock，验证后合并 main。正式 `vX.Y.Z` 标签固定到 main 事件提交，不依赖默认分支。版本号不能被另一个提交复用，正式 Release 的检查与人工升级保护不变。

## 服务器如何接收通知

GitHub → HTTPS POST `/hooks/github/releases` → Go 控制端验签 → 持久化本地任务 → 现有升级 worker。平台直接处理，不依赖 deploy-delivery，不需要 SSE、额外常驻服务或版本轮询。公网只代理这一条 POST 路径到 `127.0.0.1:8088`；管理 API 仍要求 Bearer token，不要把它们一起公开。

唯一新增凭据是 Webhook 共享密钥，分别保存到 GitHub 源仓库 Settings → Webhooks → Secret 和服务器 `/etc/deploy-platform/webhook.secret`，不是 Actions Secret。服务器首次配置示例：

```bash
sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/deploy-platform/webhook.secret'
```

仅首次创建，已有密钥不要覆盖。配置 GitHub Webhook 时，在自己的可信终端读取该文件；不要把值放入仓库、日志或消息。使用 application/json、启用 SSL verification、只选择 Releases 事件，URL 为 `https://通知域名/hooks/github/releases`。凭据数秒内热加载，默认路径不用写配置。

在单独维护的 Caddy 站点中加入以下路由；不要覆盖平台自动生成的业务代理配置：

```caddyfile
notify.example.com {
    @release {
        method POST
        path /hooks/github/releases
    }
    handle @release {
        reverse_proxy 127.0.0.1:8088
    }
    handle {
        respond 404
    }
}
```

域名需解析到服务器，HTTPS 端口按正常 Caddy 配置开放。控制端仍只监听 loopback，8088 不对公网开放。没有配置密钥时 Webhook 返回 503，普通项目管理、手动更新仍可使用。

原始正文使用 HMAC-SHA256 常量时间验签，固定仓库，只接收 release/published 的固定 dev 构建或正式版本。正文上限 256 KiB、并发上限 8，忽略草稿、其他事件和滚动 dev-latest。不使用正文中的下载地址、路径或命令。签名正确的 ping/无关事件返回 204；有效发布可靠落盘后返回 200，磁盘或队列持久化失败返回 503。HTTP 请求不等待下载安装。

最新两个通道及 dev 提交标记保存在私有 `notification-inbox.json`，原子落盘并 fsync，重启继续处理。较旧和重复版本不会推进状态。正式通知仅触发可信 GitHub Release 检查，候选必须由用户在网页或 CLI 确认；不会自动提交正式升级。启动补查一次正式版，没有每小时版本检查。

## dev 自动升级的唯一开关

缺省关闭，无需配置任何额外项。首次安装后：

```bash
# 查看当前本地策略；不显示 token
sudo cloudctl update policy
# 明确允许本机 dev 自动升级；数秒内生效
sudo cloudctl update policy --dev-auto-upgrade on
# 关闭并移除多余的布尔配置
sudo cloudctl update policy --dev-auto-upgrade off
```

策略存放在现有 `/etc/deploy-platform/updates.json`，只需一个布尔值：

```json
{"repository":"stewardful/deploy-platform","notifications":{"dev_auto_upgrade":true}}
```

CLI 保留既有仓库/正式版私有 token 配置，不打印密钥。公开 dev 下载不读取该 token。只有密钥采用非默认路径时才需添加 `notifications.secret_file`，必须是私有文件的绝对路径；不保留旧 endpoint 配置。

打开后，已持久化的新 dev 通知会提交一个有限的本地升级请求。关闭期间收到的最新通知会保留，开启后处理；没有收到的通知不会凭空执行。Go 与 CLI 共用提交锁、更新目录和独立 systemd worker。worker 再次读取本地开关、重新核对固定公开清单、架构/大小/摘要和包内来源，再走原安装器的兼容性检查、排空、备份、业务验证和失败回退。下载后、进入安装前还会再检查开关；关闭不会强行中断已开始的安装事务。

只有已在 GitHub 公开清单验证的构建才能推进 `/var/lib/deploy-platform/updates/notification-dev.json`。安装前记录 attempted，完成记 succeeded，失败记 failed；较旧、同一构建及已安装提交不会重复安装。重启或重复消息不能重试已提交构建。发现阶段的失败同样不自动循环重试；不要靠删除记录强制重试，排错后用手动 `cloudctl upgrade-dev --run-id ID --yes` 明确重试。

任务冲突时只等待本地队列空闲，不重复下载或并发安装。缺失业务探针、版本/运行时/schema 不兼容等保护不会因 dev 自动模式而跳过。正式生产节点应保持 dev 自动升级关闭。

## 状态、首次安装和验证

网页显示 Webhook 配置状态、dev 自动开关、收到的构建提示和失败记录；“收到通知”不等于安装成功。`cloudctl update status` 和 `cloudctl system-logs update` 查询真正执行状态。首次安装仍用 cloudctl，见 [安装指南](install.md#统一-cloudctl-首次安装)。

GitHub Webhook 可能漏送，服务不可用时投递会失败；可在 GitHub Recent Deliveries 手动 Redeliver。已经成功入队的通知会在平台重启后恢复。没有收到的 dev 通知用 `cloudctl upgrade-dev --yes` 明确检查并升级，不增加后台版本轮询。默认关闭 dev 自动升级；正式版始终只提示手动确认。

[可复用 Webhook 处理组件](../apps/controlplane/notifications/handler.go) 与升级实现解耦，通过固定的 Go 回调提交有限的 Notice；不依赖私有仓库 SDK。组件测试覆盖验签、来源、正文上限、持久化、并发锁、策略变更、重复与失败抑制；原 Ubuntu 升级测试继续验证业务数据保留和失败回退。

本变更未部署用户服务器；需先审核合并平台 PR，再配置 HTTPS Webhook 并首次发布 dev 包。deploy-delivery 是其他项目可选的独立验签转发工具，本平台无需安装它。新 dev-release.yml 是只发布的独立工作流，不要恢复旧已暂停的 SSH 工作流。
