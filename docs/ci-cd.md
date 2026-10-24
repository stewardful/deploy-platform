# CI、发布与本机通知升级

| 触发 | 行为 | 是否部署服务器 |
| --- | --- | --- |
| PR | 检查、构建和隔离 Ubuntu 安装/升级验证 | 否 |
| dev 推送 | 完整测试、双架构更新包、公开 dev 预发布 | 否 |
| main 推送 | 双架构完整离线包与正式 Release | 否 |

Actions 不持有服务器登录权限，不发送远程命令，也不调用服务器管理 API。没有 SSH 部署任务、主机/用户/端口/私钥/known_hosts 配置、TEST_SERVER_AUTO_DEPLOY 开关、独立 CI 交付 worker 或旧 cloudctl delivery 命令。仅接受当前包、状态和通知契约，不保留旧交付兼容路径。

## 零自定义 Actions 配置

公开构建发布使用 GitHub 自动提供的 GITHUB_TOKEN，publish job 显式声明 Contents:write 和 Actions:read；不需要自定义 Secrets、Repository variables、PAT、GitHub App 或跨仓库 CI 触发。推送 dev 的源码经过测试和 cloudctl/amd64/arm64 打包后才发布。仓库默认分支是 dev。

[dev 发布工作流](../.github/workflows/dev-release.yml) 先将完整资产上传固定 `dev-RUN_ID` 草稿，再公开预发布，固定标签指向 dev 事件 SHA。已公开资产不可由流程覆盖，重跑冲突需新构建。`dev-latest` 只提供最新发现清单及 cloudctl，安装包始终从固定标签下载。旧源码或旧构建重跑不回退最新指针，预发布不成为正式 latest。

固定构建包含两架构平台更新包、摘要、cloudctl、dev.json；dev 包不重复打入基础运行时，按版本下载或复用。Release 附件不受 Actions 三天缓存期限影响，暂无自动清理；被删除的构建不能按编号恢复。正式版本仍保留完整离线包。

main 发版前提高 workspace 和内部组件版本，更新 Cargo.lock，验证后合并 main。正式 `vX.Y.Z` 标签固定到 main 事件提交，不依赖默认分支。版本号不能被另一个提交复用，正式 Release 的检查与人工升级保护不变。

## 服务器如何接收通知

[deploy-delivery 通知 API](https://github.com/stewardful/deploy-delivery) 是独立低权限服务。可部署在同一台 Ubuntu 上，通过 Caddy 只公开一个 HTTPS Webhook 路径；HMAC 密钥只配置在 GitHub 仓库 Webhooks 与该服务的私有文件中。它只提供消息，不执行 cloudctl，不持有平台权限。不是 Actions Secret。

Go 控制端默认连接本机 `http://127.0.0.1:8090/v1/events`，一条 SSE 长连接接收完整 snapshot，15 秒心跳由通知服务提供。连接断开带退避重连，读取有超时，帧大小有上限，拒绝公网地址、主机名、跳转及请求中提供的下载地址。没有新常驻平台服务或定时版本轮询。API 未部署时，普通项目管理、手动更新仍可使用，网页显示通知未连接。

消费者只接受本机配置仓库的消息。正式版通知只触发受限的可信 GitHub Release 检查，网页显示候选，必须用户在网页或 CLI 确认。通知不能提交正式升级。启动和 SSE 重连会补查一次正式版；不存在每小时自动版本检查。

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

CLI 保留既有仓库/正式版私有 token 配置，不打印密钥。通知和公开 dev 下载不读取该 token。通知 API 使用非默认本机端口时才需添加 `notifications.endpoint`，必须是 loopback IP 的 `/v1/events`，修改地址后重启 deploy-control；默认地址不用填写。

打开后，启动、SSE 重连或新 dev 消息会提交一个有限的本地检查请求。Go 与 CLI 共用提交锁、更新目录和独立 systemd worker。worker 再次读取本地开关、重新核对固定公开清单、架构/大小/摘要和包内来源，再走原安装器的兼容性检查、排空、备份、业务验证和失败回退。下载后、进入安装前还会再检查开关；关闭不会强行中断已开始的安装事务。

只有已在 GitHub 公开清单验证的构建才能推进 `/var/lib/deploy-platform/updates/notification-dev.json`。安装前记录 attempted，完成记 succeeded，失败记 failed；较旧、同一构建及已安装提交不会重复安装。重启、重复消息或消费者重连不能重试已尝试构建。发现阶段尚未核验的网络失败不推进记录，可在后续重连补查；不要靠删除记录强制重试，排错后用手动 `cloudctl upgrade-dev --run-id ID --yes` 明确重试。

任务冲突时只等待本地队列空闲，不重复下载或并发安装。缺失业务探针、版本/运行时/schema 不兼容等保护不会因 dev 自动模式而跳过。正式生产节点应保持 dev 自动升级关闭。

## 状态、首次安装和验证

网页显示通知连接状态、dev 自动开关、收到的构建提示和失败记录；“收到通知”不等于安装成功。`cloudctl update status` 和 `cloudctl system-logs update` 查询真正执行状态。首次安装仍用 cloudctl，见 [安装指南](install.md#统一-cloudctl-首次安装)。

GitHub Webhook 可能漏送；消费者在启动/重连时补查可信发布源。持续断线时不会每分钟查版本；可在本机手动检查。通知服务失败投递可在 GitHub Recent Deliveries 重投。dev 本地开关关闭时不检查或安装 dev 包；正式版始终只提示手动确认。

[Go SSE 客户端](../apps/controlplane/notifications/client.go) 不依赖私有仓库 SDK；两仓库仅通过本机 HTTP/SSE 契约连接，无 GitHub App 或相互触发。组件测试覆盖消息、超时、来源、并发锁、策略变更、重复与失败抑制，原 Ubuntu 升级测试继续验证业务数据保留和失败回退。

本变更未部署用户服务器；需先审核合并两仓库相关 PR，再安装通知服务、配置 HTTPS Webhook 并首次发布 dev 包。新 dev-release.yml 是只发布的独立工作流，不要恢复旧已暂停的 SSH 工作流。
