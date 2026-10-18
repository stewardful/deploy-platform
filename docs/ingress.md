# 域名、反向代理与自动 HTTPS

本文适用于 0.5.0。自 0.4 起，平台通过独立 Caddy 进程处理 TLS、ACME 签发/续期和 HTTP/WebSocket 代理；与 Go 控制端、Rust Agent、nerdctl/containerd 配合，目标服务器不编译源码，也不需要 Docker、Compose、额外 DNS 服务或每项目一个代理进程。

## 使用步骤

1. 安装或升级对应架构的 0.5.0 `.run`。已有安装使用 `sudo bash deploy-platform-0.5.0-linux-amd64.run --upgrade`（ARM64 改用 arm64 包）。安装器会备份应用和域名配置数据库，保留应用数据、密钥、令牌与证书存储。
2. 将域名 A 记录解析到服务器公网 IPv4。仅在服务器具备可用公网 IPv6 时配置 AAAA；错误的 AAAA 也可能导致签发失败。
3. 腾讯云安全组与主机防火墙放行 **TCP 80、443**；服务器出站需能访问 DNS 和 ACME 服务。80/443 不应由其他程序占用。平台不修改云账户 DNS、安全组或已有 Nginx。
4. 目标应用使用 `network: "project"`，服务在容器内监听 `0.0.0.0:<端口>`。不必映射宿主端口。旧 `bridge`/`none` 应用需要以新应用名重新部署到项目网络，第一版不支持原地修改已有应用配置。
5. 网页“域名与 HTTPS → 绑定域名”，选择应用、填写**容器 HTTP 端口**，选择正式证书并保存。
6. 等待“代理已同步”，再“查看证书”。只有“证书已就绪（系统信任）”才说明本机代理实际提供了通过域名、有效期与系统信任链验证的证书。首次签发可能需要数分钟。

腾讯云中国大陆节点的域名公网服务需按实际地域和用途完成备案要求，参考 [腾讯云备案判断说明](https://cloud.tencent.com/document/product/243/19630)。这与 TLS 证书签发是分别处理的前置条件。

示例：`app.your-domain.com → frontend:8080`、`api.your-domain.com → backend:8080`。跨域浏览器请求的 CORS 白名单由后端应用设置；平台不自动添加允许所有来源的响应头。也可保留已有前端项目对 `/api` 的内部转发，使用同一个前端域名访问完整链路。

第一版按**完整域名**绑定一个应用端口，保留请求路径；暂不提供同一域名多路径编排、通配符/DNS-01、外部地址代理、手动上传私钥、自动配置 DNS、负载均衡集群或 CDN 接入。

## 模块和复用

| 模块 | 职责 |
|---|---|
| `ingress-model` | 精确域名、绑定、版本、状态与 `Proxy` 端口；无 HTTP、SQL、运行时实现 |
| `ingress` | 独立 SQLite WAL 存储、版本冲突保护、意图与实际状态协调；通过 Agent 内部端口取得合法上游，不依赖 Caddy |
| `ingress::caddy` | Caddy JSON 编译与受限 Unix Socket 管理适配器；复用模块内 `bootstrap.json` 作为安装器与 Agent 的共同配置源 |
| `runtime-api` / `runtime-nerdctl` | 复用容器归属、配置指纹、项目网络和私有 IPv4 校验；健康检查和域名代理共用同一个地址解析实现 |
| `agent-api` | 域名配置、查询与删除合约；上游地址解析是内部组合端口，没有公开 HTTP 入口 |
| `packages/go/agentapi` | 可复用的域名管理 SDK |
| Go `certificates` | 对固定本机代理做 TLS 握手，分别验证域名、时间和信任；不读取私钥 |
| `packages/ts/control-client` / Web `IngressPanel` | 绑定、编辑、启停用、移除、同步状态和证书检查；域名操作不混入容器部署任务 |

域名配置存储于 `/var/lib/deploy-platform/ingress/ingress.sqlite`，由持有节点租约的 Agent 管理。64 个绑定上限，完整配置采用版本校验；旧页面不能覆盖新版本。POST 返回 202 和持久化 revision，表示**意图已保存**，尚不保证代理或证书就绪。删除也以 `revision == applied_revision` 为同步完成依据。

Agent 收到变更立即唤醒协调器；另每 10 秒核对代理配置和实际容器地址。同一轮复用同一应用的目标查询。容器重建后重新解析目标，代理单独重启后从数据库恢复配置。配置没有变化时不重复 reload。上游不可用时生成 503，不回退到任意地址。代理不可用或加载失败显示 `proxy_unavailable` 并自动重试；Caddy 加载失败时保留上一份有效配置。

同步是最终一致的：停用、移除、上游状态/IP 变化可能有一个协调周期的延迟，现存连接也可能继续完成；不承诺即时撤销或零延迟切换。节点仍面向单管理员与可信镜像，项目网络和域名绑定不是恶意多租户的强安全边界。

## 安全边界

- 只接受小写精确 DNS 名称，国际化域名使用 punycode；拒绝 URL、端口、路径、IP、通配符、换行和常见私有域名后缀。
- 请求只包含已登记 AppKey 和容器端口；不接受上游 URL、任意主机 IP、Caddyfile、代理头、证书路径或命令。
- Rust 核实应用配置、容器归属、指纹、项目网络和私有 IPv4；绑定不允许代理到控制端、云元数据地址或主机 Socket。
- Caddy 只管理显式登记域名，不启用任意 SNI 的 on-demand 签发；启用 SNI/Host 一致性校验。只提供 TCP HTTP/1.1、HTTP/2，不要求开放 UDP 443。
- Caddy 管理接口位于 `/run/deploy-platform-proxy/admin.sock`，父目录 0700，无公网或 TCP 管理端口。Go 管理接口保持原有回环监听、令牌认证及 SSH 隧道使用方式。
- Caddy 使用 DynamicUser，权限仅保留低端口绑定能力，系统文件只读，无法读取 Agent 状态、卷、密钥与管理令牌目录。
- 发布目录只有程序和公共配置，目录 0755；应用数据、密钥和证书在各自私有目录，不能用发布目录权限替代秘密存储权限。
- 代理连接超时 3 秒、上游响应头超时 30 秒；流式正文与 WebSocket 交给成熟代理实现。应用仍应自行实现登录、请求限流、上传大小限制和 CORS。

## HTTPS 生命周期

正式模式使用 Let's Encrypt production ACME，测试模式使用 staging；测试证书通常不被浏览器信任。Caddy 自动处理 HTTP-01/TLS-ALPN-01、重试、续期与证书缓存。签发失败不等于域名配置未保存，也不会自动重启业务应用。

Caddy 重启以只含私有管理接口的 bootstrap 启动，不恢复可能过期的运行时 IP 配置；Agent 校验应用后重建路由。证书、ACME 账户和私钥继续保存在 Caddy 自己的持久化存储中，路径为 `/var/lib/deploy-platform-proxy/caddy`（DynamicUser 下可能由 systemd 映射至 `/var/lib/private`）。停用或移除域名保留缓存，不触发证书撤销或删除私钥。

安装升级保留该存储；**保留不等于备份**，安装器不复制证书私钥作为普通升级备份。需要恢复保障时由管理员妥善备份此目录及域名数据库，私钥备份应限制权限和妥善加密。

证书检查只向 `127.0.0.1:443` 发起带已登记 SNI 的握手，不连接用户提交的外部地址。初始握手允许读取证书，随后显式验证域名、时间和完整信任链；不会把“拿到了证书”误报为系统信任。返回元数据，不返回证书私钥。

参考：[Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)、[管理 API 与私有 Unix Socket](https://caddyserver.com/docs/api)。

## CLI 与 API

`route.json`：

```json
{
  "domain": "app.your-domain.com",
  "app": {"project":"my-project","name":"frontend"},
  "port":8080,
  "tls":"production",
  "enabled":true
}
```

```sh
sudo cloudctl route-set --file route.json --expected-revision 0
sudo cloudctl routes
# 编辑用查询得到的该绑定 revision，首次创建使用 0。
sudo cloudctl route-delete --domain app.your-domain.com --expected-revision 1
sudo cloudctl logs proxy
```

| 操作 | Agent Unix Socket | Go 管理 API |
|---|---|---|
| 查询绑定和同步状态 | `GET /v1/ingress` | `GET /api/v1/ingress` |
| 创建/编辑 | `POST /v1/ingress`，`{route,expected_revision}` | `POST /api/v1/ingress` |
| 移除 | `POST /v1/ingress/delete`，`{domain,expected_revision}` | `POST /api/v1/ingress/delete` |
| 实际证书检查 | 由 Go 完成 | `GET /api/v1/ingress/certificate/{domain}` |

域名变更使用持久化版本校验，不是容器任务；断线后先刷新核对状态，再按当前 revision 编辑。网页域名配置不写浏览器本地存储。

## 排查

- **代理同步失败**：检查 `cloudctl logs proxy`、`systemctl status deploy-proxy`、80/443 占用，以及安装目录权限。不要把整个状态目录改成 755。
- **证书未就绪**：核对 A/AAAA、CNAME 链、安全组、主机防火墙、出站 DNS/HTTPS、系统时间与 CA 错误/限额；先用 staging 排查。管理 API 的“代理已同步”不代表签发完成。
- **证书存在但不信任**：检查是否选了 staging、客户端系统根证书是否正常、代理是否仍提供旧证书。平台不通过关闭浏览器证书校验解决此问题。
- **HTTPS 502/503**：检查应用正在运行、端口正确、服务监听 0.0.0.0、项目网络和应用日志。`上游运行`仅表示已解析到归属正确的运行实例，不代表业务健康；HTTP 健康状态仍在应用诊断面板查看。
- **跨域错误**：由后端仅允许实际前端 HTTPS Origin，或在前端项目中转发 `/api`；平台不生成全来源通配 CORS。

## 验收范围

隔离 Ubuntu 的真实 ACME 测试使用本地 CA，并在**该测试容器内**信任其公钥。已验证证书申请的 HTTP 挑战、前端/后端 HTTPS、数据库/缓存读回、续期触发、重启恢复、503/恢复、严格 SNI 和版本冲突。测试 CA、hosts 和特殊 Agent CA 覆盖均不属于发布包的生产配置。

没有连接真实腾讯云服务器或用户域名，因此不把本地 CA 证书说成已经拿到公网 Let's Encrypt 证书；正式签发需要上述公网前置条件。生产续期时间策略由 Caddy 默认行为管理；续期验收只在隔离测试中加速检查间隔和窗口，不改变发布配置。
