# 统一命令入口

一键安装 0.5.2 后，使用 `sudo cloudctl` 查看分类帮助。日常应用操作直接写子命令，不必先记住 `cloudctl agentctl ...`；原写法仍支持。

```bash
sudo cloudctl help
sudo cloudctl help deploy
sudo cloudctl apps
sudo cloudctl apply -f app.toml
sudo cloudctl deploy -f app.json
sudo cloudctl start --project demo --name web
sudo cloudctl stop --project demo --name web
sudo cloudctl diagnostics --project demo --name web
sudo cloudctl logs --project demo --name web --tail 50
sudo cloudctl job 1
sudo cloudctl routes
```

apply/deploy 默认等待任务终态；`--no-wait` 返回已持久化任务。终态失败仍返回失败，不把 HTTP 202 当成部署成功。JSON 必须是普通 AppSpec，和 TOML 共用校验；带 request_id/spec 外壳的 HTTP 请求样例不是配置文件。

```bash
sudo cloudctl secret-create --project demo --name api-key --file /私有路径/key.txt
sudo cloudctl secrets --project demo
sudo cloudctl volumes --project demo --name web
sudo cloudctl route-set --file route.json --expected-revision 0
sudo cloudctl route-delete --domain app.your-domain.com --expected-revision 1
```

密钥从文件读取，不放在命令行参数中。域名编辑/删除必须使用实际查询到的版本，详细说明见 [域名指南](ingress.md)。没有新增删除应用数据或全局清理命令。

## 三种状态与日志

| 命令 | 含义 |
| --- | --- |
| `cloudctl status` / `verify` | 整个安装节点的运行时、Agent、Go/Web、代理及认证自检 |
| `cloudctl doctor` | 只检查 Agent API/worker，不依赖 Go/Web；不表示业务健康 |
| `cloudctl diagnostics --project P --name APP` | 指定应用实际状态与按需 HTTP 就绪检查 |
| `cloudctl logs --project P --name APP` | 应用 stdout/stderr，有行数/大小限制 |
| `cloudctl system-logs agent` | 节点系统服务日志；也可选 control/runtime/proxy |

旧的 `cloudctl logs agent` 等写法继续读系统日志；新文档推荐 `system-logs`，避免与应用日志混淆。`cloudctl runtime ...` 仅供管理员排错，不代替正常启停意图管理。

## 独立 Agent

独立安装 agentd/agentctl 不需要 cloudctl、Go 或 Web：

```bash
agentctl --help
agentctl help deploy
agentctl --socket /run/deploy-platform/agent.sock apps
agentctl --socket /run/deploy-platform/agent.sock apply -f app.json
```

`validate/plan` 可以离线运行；其他命令需要常驻 agentd。`agentctl status` 是 doctor 的别名，与 cloudctl 的全节点 status 不同。独立模式的 nerdctl 路径/Socket/namespace 仍由 agentd 管理员参数配置，CLI 不自行打开数据库。

服务安装方式见 [独立 Agent](agent.md)、[一键安装](install.md)。命令与网页使用同一个 Agent 写入者、持久化任务与部署实现；Go/Web 停止不会阻止 Agent 管理和恢复应用。

升级维护命令为 `sudo cloudctl maintenance enter|status|exit`，状态区分门禁、待完成任务和 quiescent；升级流程中不要手动解除门禁。配置与恢复说明见 [升级保障](upgrade.md)。
