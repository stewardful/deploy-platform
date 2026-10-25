# CI、发行与独立 Webhook 执行

| 仓库/组件 | 职责 |
| --- | --- |
| deploy-platform monorepo 的 cloudctl | 下载、校验、幂等安装/升级、状态、独立执行器，归属本仓库 |
| deploy-platform Web / Go / Rust Agent | 管理服务器项目、数据、网络和手动正式升级；没有发布 Webhook 接口 |
| [deploy-webhook](https://github.com/stewardful/deploy-webhook) | 独立 HTTP 验签、持久化通知，后台固定调用本机 cloudctl |

两个仓库 CI 独立，无跨仓库触发、私有 SDK、SSH、PAT、GitHub App 或自定义 Actions Secret。平台 dev 推送通过测试、双架构构建后发布固定 `dev-RUN_ID`，随后更新 `dev-latest`；main 发布固定 `vX.Y.Z` 与完整离线包。公开资产不回填、不覆盖；旧源码重跑不能回退最新指针。正式版本号不能被其他提交复用。

CLI artifact 和 Release 包含 cloudctl、install.sh 及各自摘要。入口固定同构建 CLI 地址与 SHA256；发布前核对来源，不混用滚动文件。dev 包按版本下载/复用基础运行时，正式离线包保留。Actions 缓存不是服务器安装源，公开 Release 下载无需 token；删除构建后不能按原编号恢复。

## 一次 dev 变更如何安装

```text
合并 dev → CI 成功 → 固定 dev-RUN_ID Release published
  → GitHub Releases Webhook → deploy-webhook 验签并落盘
  → 无 HTTP 入口的 root oneshot 复核签名、来源、构建、提交与去重
  → cloudctl install --channel dev --run-id ID --commit SHA --automatic
  → 已有保护安装器：检查、排空、备份、验证或回退
```

接收 Release published 而不是刚 push 就安装：代码合并时该次产物尚未就绪。通知携带固定构建与完整提交；cloudctl 再核对公开清单和包内来源，不能拉到上一版或执行正文中的 URL/命令。平台内没有通知监听、SSE 或后台版本轮询。

HTTP 进程使用 DynamicUser，无 root、sudo 或平台目录权限；仅写自己的私有收件箱。systemd.path 唤醒短期 root worker，执行路径和参数固定，没有任意脚本/命令接口。收件箱可由低权限进程写入，因此 root worker 再验签；执行账本由 root 独占，attempted 先落盘，失败或重启不重复执行。

## 本地开关

cloudctl 可先独立安装，再部署平台。自动 dev 安装默认关闭：

```bash
sudo cloudctl update policy
sudo cloudctl update policy --dev-auto-upgrade on
sudo cloudctl update policy --dev-auto-upgrade off
```

策略位于 `/etc/deploy-platform/updates.json`，当前结构为：

```json
{"repository":"stewardful/deploy-platform","token_file":"","dev_auto_upgrade":false}
```

Webhook 不能开启此开关。自动调用必须同时指定 dev 构建与完整 SHA；稳定版和任意本地文件不能用 automatic 模式。开关在提交前及下载后安装前复核，关闭不会强行中断已开始的安装事务。没有通知服务设置、旧 endpoint 或 secret_file 兼容。

## 正式版与排错

正式发布只记录为待人工处理，不自动执行安装。平台页面保留启动时/手动检查正式版本与明确确认；CLI 对已安装节点的正式或本地更新也要求 `--yes`。

```bash
sudo cloudctl install --channel stable --version X.Y.Z --yes
sudo cloudctl update status
sudo cloudctl system-logs install
sudo deploy-webhook --status
```

下载失败、签名不符、提交不符、缺少探针、schema/运行时不兼容都不能跳过保护。失败先查日志；需要重试时管理员手动执行固定 cloudctl install，不清除去重账本强制自动重试。任务按 ID 保存结果，后续提交不会覆盖前一个等待者的终态；保留最近 100 项。

Webhook 不是保证送达的队列：停机期间失败投递可在 GitHub Recent Deliveries 重投；已成功保存的通知通过 worker 恢复处理。开关关闭时记 blocked，开启后处理下一次有效发布；也可手动安装已跳过构建。不会每分钟拉版本。

## 配置与发布状态

通知组件只需一份 Webhook HMAC 密钥（服务器私有文件和 GitHub 仓库 Settings → Webhooks），以及可用的 HTTPS 入口；不是 Actions Secret。平台不保存 Webhook 密钥，也不依赖通知仓库。

两个仓库相关变更尚待审核。未部署用户服务器、未设置真实 Webhook。install.sh 新行为需本 PR 合并并成功发布后可用；不要把本地隔离 HTTPS/容器测试当作腾讯云公网验收。
