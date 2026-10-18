# 0.5 首次上云验收

目标：证明安装包在你自己的服务器上能部署、访问和恢复项目。已有的本地验收记录不能替代这一步。建议先使用没有业务数据的 Ubuntu 24.04 测试节点，按下面顺序执行；不要在 2 GiB 节点同时运行多套自检项目。

逐步安装、准备示例镜像、创建密钥、导入四应用 JSON、绑定域名的完整流程见 [腾讯云 Ubuntu 操作手册](tencent-ubuntu-walkthrough.md)。

## 1. 安装与基础检查（约 10 分钟，视网络而定）

按 [安装指南](install.md) 上传对应 CPU 架构的 `.run` 和可信来源的 `.sha256`。以 amd64 为例：

```bash
bash deploy-platform-0.5.0-linux-amd64.run --check
sha256sum --check --ignore-missing deploy-platform-0.5.0-linux-amd64.sha256
sudo bash deploy-platform-0.5.0-linux-amd64.run
sudo cloudctl verify
sudo systemctl is-active deploy-containerd deploy-agent deploy-control deploy-proxy
curl --noproxy '*' -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/api/v1/apps
```

通过标准：包校验成功、`verify` 输出 PASS、四个服务均 active、未带令牌的应用接口返回 **401**。管理端保持回环监听，通过安装指南的 SSH 隧道打开网页；不要开放 8088 作为公网业务入口。

## 2. 已提供的真实节点自检

这些不是纯模拟测试，会真实拉镜像、部署容器及短暂重启 Agent。在测试节点逐项运行，前一项结束后再执行下一项。

| 命令 | 验证内容 | 通过标准与副作用 |
|---|---|---|
| `sudo cloudctl smoke` | 普通 Web 应用、HTTP、资源限制、请求幂等、Agent 恢复 | 输出 PASS；最后停止自己的测试应用；保留停止元数据及镜像缓存 |
| `sudo cloudctl stateful-smoke` | PostgreSQL/Redis 认证读写、容器重建后数据保留、停止意图 | 两种数据库均输出 PASS；会重启 Agent；最后停止测试应用，保留卷和密钥 |
| `sudo cloudctl fullstack-smoke --image YOUR_IMAGE --keep-running` | 前端→后端→PostgreSQL/Redis、故障诊断、项目隔离、重建恢复 | 全部 PASS；保留运行中的四服务项目，用于后续网页和 HTTPS 验收 |

`YOUR_IMAGE` 不是公共现成镜像。需要先按 [完整部署指南](fullstack.md#构建示例镜像在开发机ci) 在开发机/CI 构建并推送示例镜像，再替换为服务器能拉取的真实镜像地址。服务器不编译源码。私有仓库凭据管理尚未提供。

也可以导入你已经构建、且与服务器架构一致的镜像：

```bash
sudo cloudctl runtime load -i fullstack-demo.tar
sudo cloudctl fullstack-smoke --image docker.io/library/cloud-fullstack-demo:v1 --pull-policy never --keep-running
```

镜像名称必须与导入包里的标签一致。记录输出中的 `stack-...` 项目名、前端回环地址。若失败，先查看该项目的任务、诊断与日志，不要反复运行创建更多测试项目。

## 3. 网页和公网 HTTPS 用例

最简验收不依赖四服务镜像：网页部署 `docker.io/nginxinc/nginx-unprivileged:stable-alpine`，项目 `acceptance`、应用 `frontend`，选择 **project 网络**、128 MiB、250 millicpu、64 PID，健康检查端口 8080、路径 `/`。无需映射宿主端口。若使用上一步四服务项目，直接选择其 frontend，容器端口也是 8080。

先按 [域名指南](ingress.md#使用步骤) 准备一个真实测试子域名，解析到服务器，放行 TCP 80/443。不要将 `example.com` 或他人域名用于公网申请。

| 编号 | 操作 | 预期结果 |
|---|---|---|
| W01 | 网页部署，查看任务与诊断 | 任务成功，HTTP 健康；日志能查看 |
| W02 | 绑定测试域名到 frontend:8080，选择正式证书 | 先保存，再代理已同步；证书最终显示系统信任 |
| W03 | 在自己电脑浏览器访问 HTTP 与 HTTPS 地址 | HTTP 跳 HTTPS；HTTPS 无证书警告，页面可访问 |
| W04 | 若有四服务项目，页面写入并读回数据 | PostgreSQL 计数和 Redis 读取值一致；记录结果 |
| W05 | 网页停止前端，等待状态收敛，再启动 | 停止后新请求 503；启动后恢复；不要以旧标签页缓存判断 |
| W06 | 停用域名，等待同步完成，再启用 | 停用后新请求不再得到业务页面；重新启用后恢复；停用时可能 TLS 拒绝或 404 |
| W07 | 查看证书信息 | 域名正确、有效期正常、正式模式系统信任；“代理已同步”本身不足以通过 |
| W08 | 移除域名，等待全局 revision 与 applied_revision 一致 | 新请求不再代理到应用；应用与数据本身仍保留 |

W08 留到重启/升级验收之后执行，避免删除后还期待 HTTPS 恢复。停用、移除有协调延迟，旧连接也可能继续完成。

在**自己的电脑**执行以下请求，检查公网链路。将域名替换为刚绑定的真实域名；不要使用 `-k` 跳过证书校验：

```bash
curl --noproxy '*' -I http://app.your-domain.com/
curl --noproxy '*' -I https://app.your-domain.com/
```

Nginx 示例预期 HTTP 为 301/308 且 Location 指向 HTTPS，HTTPS 为 200。自己的业务应用允许按业务返回其他预期状态。网页本机证书检查不能单独证明公网 DNS、安全组或客户端链路可用。

## 4. 重启与升级验收（会中断测试节点服务）

仅在专用测试节点、已保存测试数据且 SSH 能重新连接的情况下执行：

1. `sudo systemctl restart deploy-agent`：等待协调，网页应用/域名仍在，HTTPS 恢复，数据仍能读回。
2. `sudo systemctl restart deploy-proxy`：等待协调，HTTPS 恢复，仍提供受信任证书。
3. 网页停止一个独立测试应用，保留另一个应用运行。执行 `sudo reboot`，重新连接后运行 `sudo cloudctl verify`：运行中的应用自动恢复，主动停止的应用仍停止，原域名和数据库数据仍在。
4. 在一次性测试节点做安装升级/重装演练：记录应用、域名、数据与登录状态，执行 `sudo bash deploy-platform-0.5.0-linux-amd64.run --upgrade`，再次运行 verify 并访问原 HTTPS/数据。相同包只能验证重装/保留路径；真正跨版本升级需旧版先有数据，再安装新版。

不要在正式节点通过杀数据库、删除运行时目录、强制断电或加速证书续期来做首次验收。数据保留测试也不代表已经验证备份恢复。

## 5. 源码回归（开发机/CI，不在 2 GiB 服务器编译）

在仓库根目录，使用项目要求的 Rust/Go/Node 工具链：

```bash
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
make go-integration go-vet
npm ci
npm test
make web-e2e
make installer-test
```

浏览器首次运行需在开发机安装 Playwright Chromium：`npx playwright install chromium`，Linux 还需其系统依赖。浏览器回归使用真实 Go/Rust API，但容器运行时是替身；不能代替上面真实节点自检。Rust 默认忽略的 Linux 独立测试也不算已经通过。

`distribution/tests/https-runtime.py` 仅供带专用标记、本地受信 ACME CA 的**可删除隔离节点**使用，会修改临时续期参数并重启服务，禁止直接在用户云服务器运行。真实公网的首次签发按 W02/W03 验收，自动续期需后续观察实际到期时间变化；首次签发成功不等于已经验证了公网续期。

## 6. 记录结果与失败排查

每项记录：安装包版本/架构、服务器系统、测试时间、用例编号、实际结果、PASS/FAIL。可保留网页截图、任务 ID、状态码、证书到期时间；不要在截图或报告里放 token、密码、私钥。

```bash
sudo cloudctl status
sudo cloudctl logs agent
sudo cloudctl logs control
sudo cloudctl logs proxy
sudo cloudctl routes
free -h
df -h
sudo cloudctl runtime system df
```

先验证一个项目，再按真实流量观察 24–72 小时的内存、磁盘与失败情况；这是初步稳定性观察，不是压力容量证明。2 GiB 节点不要同时保留多套四服务验收。结束后从网页停止相应测试项目；当前脚本保留的卷、密钥、镜像和元数据不会自动全部清理，不要使用全局删除命令处理。

已有执行证据：[0.4 HTTPS](ingress-validation.md)、[四服务链路](fullstack-validation.md)、[安装与有状态应用](install-validation.md)。本清单尚未在你的腾讯云机器执行，只有你实际记录通过后，才能称为该节点验收通过。
