# 腾讯云 Ubuntu：从安装到前后端、数据库和 HTTPS

本手册面向当前 dev 的 Ubuntu 24.04/26.04 LTS、2 核 2 GiB、单管理员节点；升级流程与必需业务探针见 [升级保障](upgrade.md)。先在没有业务数据的测试服务器执行。本教程使用仓库自带的四服务示例：前端转发 `/api` 到后端，后端通过项目服务名连接 PostgreSQL 和 Redis。通过后再换成自己的业务镜像。

命令分别标明在“电脑”还是“服务器”执行。`USER`、`SERVER_IP`、`YOUR_DOMAIN` 都是占位符，必须替换成实际值。不要把示例域名拿去申请公网证书。

## 1. 准备服务器、域名与文件

服务器需要有公网 IPv4、SSH 登录权限、sudo 权限、可用 DNS/出站网络，以及镜像和数据磁盘空间。建议至少留 10 GiB 可用空间；2 GiB 内存不要同时运行多套验收项目。此流程不安装 Docker，也不在服务器编译 Go/Rust/前端。

在腾讯云控制台为该实例配置入站规则：SSH 端口仅允许你的管理地址，TCP 80/443 用于公网网站。不开放 8088、5432、6379、2019。CVM 的操作参考 [腾讯云添加安全组规则](https://cloud.tencent.com/document/product/213/112614)；轻量应用服务器使用它自己的防火墙入口。如果服务器启用了 UFW 或其他防火墙，也需允许 SSH 和 TCP 80/443，避免修改规则时锁住 SSH。

准备两个子域名，如 `app.YOUR_DOMAIN`、`api.YOUR_DOMAIN`，A 记录都指向服务器公网 IPv4。没有可用公网 IPv6 时不要添加 AAAA。首次验收使用直接 DNS 解析，不加 CDN/第三方代理。中国大陆节点按 [腾讯云备案说明](https://cloud.tencent.com/document/api/243/19630) 完成适用的备案前置条件。

登录服务器查看架构：

```bash
uname -m
df -h /
free -h
sudo ss -lntp
```

`x86_64` 选 amd64；`aarch64` 选 arm64。已有 Nginx/其他程序占用 80/443 时先协调迁移，不要直接覆盖或停止已有业务。

电脑准备：对应架构的四服务示例镜像归档和四份应用 JSON；平台安装包由服务器自动下载。镜像/JSON 的生成见本文第 3 步。

## 2. 安装平台并进入网页

**服务器**一行安装（需一行入口改动合并并成功发布后执行）：

```bash
bash -o pipefail -c 'curl -fsSL --connect-timeout 20 --max-time 120 https://github.com/stewardful/deploy-platform/releases/download/dev-latest/install.sh | sudo bash -s -- --channel dev'
```

脚本自动校验、安装并检查服务，最终显示 `Installation verified. Dev automatic upgrades: OFF.`。不需要电脑下载上传、GitHub token 或 deploy-delivery。已有安装会拒绝覆盖，后续使用 `sudo cloudctl upgrade-dev --yes`；失败先查询状态和安装日志，详见 [一行安装与排错](install.md#统一-cloudctl-首次安装)。

```bash
sudo cloudctl status
sudo cloudctl version
sudo cloudctl token
```

成功标准：status 输出 PASS，版本为预期 dev 构建。token 仅自己保存，不发到聊天或截图。

**电脑**新开终端，保持该隧道运行：

```bash
ssh -N -L 8088:127.0.0.1:8088 USER@SERVER_IP
```

浏览器打开 `http://127.0.0.1:8088/`，输入 token。这里管理的是服务器。业务 HTTPS 配置完成后，管理页面仍通过隧道访问。

可先在服务器执行 `sudo cloudctl test smoke`。它会真实部署普通应用、重启 Agent 并验证恢复，最后停止自己的测试应用；应在测试节点执行。

## 3. 在电脑或 CI 准备示例镜像和 JSON

此步骤在**仓库根目录的开发电脑/CI**执行，需要 Go 1.26.9、Python 3 和可构建 Linux 镜像的 Docker。Docker 只用于开发构建，服务器不依赖 Docker。

以下为 amd64 服务器；arm64 将 GOARCH 改为 arm64，Docker 平台改为 linux/arm64，归档文件名也改为 arm64：

```bash
mkdir -p target/fullstack-build target/tencent-acceptance/specs
GOWORK=off GOTOOLCHAIN=go1.26.9 CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go -C examples/fullstack/service build -trimpath -ldflags='-s -w' \
  -o ../../../target/fullstack-build/fullstack-demo .
cp examples/fullstack/service/Dockerfile target/fullstack-build/Dockerfile
docker build --platform linux/amd64 -t cloud-fullstack-demo:0.4-acceptance target/fullstack-build
docker save -o target/fullstack-demo-0.5.0-linux-amd64.tar cloud-fullstack-demo:0.4-acceptance
python3 examples/fullstack/generate.py --project acceptance \
  --image docker.io/library/cloud-fullstack-demo:0.4-acceptance \
  --pull-policy never --output target/tencent-acceptance/specs
scp target/fullstack-demo-0.5.0-linux-amd64.tar USER@SERVER_IP:~/
```

**服务器**导入镜像：

```bash
sudo cloudctl runtime load -i ~/fullstack-demo-0.5.0-linux-amd64.tar
sudo cloudctl runtime images
```

确认有 `cloud-fullstack-demo:0.4-acceptance`。它同时提供 frontend/backend 两种角色。PostgreSQL、Redis 镜像仍需要服务器从镜像仓库拉取；示例包不是完全断网安装包。

电脑保留 `target/tencent-acceptance/specs/` 的四份 JSON，下一步从网页导入。每个配置都是 project 网络；只有 frontend 发布到服务器回环端口 18080，数据库不对公网开放。已有 18080 被占用时，生成配置时添加 `--port 另一个空闲端口`，并对应修改下文隧道。

## 4. 创建密钥并依次部署四个应用

在电脑生成两份独立随机密码，例如分别运行两次 `openssl rand -hex 24`，保存在自己的密码管理器，勿写入 JSON 或普通环境变量。记为数据库密码 D、Redis 密码 R。

网页“数据与密钥”，项目填写 **acceptance**：

| 密钥名 | 类型与内容 |
|---|---|
| `db-password` | 普通文本，内容为 D，无额外空格/换行 |
| `redis-password` | 普通文本，内容为 R |
| `redis-config` | 选择 Redis 密码类型，输入相同 R；页面自动生成 requirepass 配置 |

当前同名密钥内容不可修改；若输错应使用新的测试项目及配置重新开始，不能通过覆盖来改数据库密码。

“应用管理 → 部署应用 → 导入应用 JSON”，按顺序导入电脑上的：

1. `postgres.json`：等待任务成功，再看日志确认数据库就绪。未配置 HTTP 健康检查是正常的。
2. `redis.json`：等待任务成功、日志显示可以接受连接。
3. `backend.json`：等待任务成功，诊断中 HTTP `/health` 为 healthy。
4. `frontend.json`：等待任务成功，诊断中 HTTP `/health` 为 healthy。

数据库资源上限分别为 PostgreSQL 384 MiB、Redis 256 MiB；后端 128 MiB、前端 64 MiB。启动成功不等于业务已健康，以诊断/访问结果为准。

**电脑**新开业务隧道：

```bash
ssh -N -L 18080:127.0.0.1:18080 USER@SERVER_IP
```

打开 `http://127.0.0.1:18080/`，点击页面写入按钮。通过标准：返回 PostgreSQL 计数与 Redis 读取值一致，并能继续写入。先完成这一步再配域名，避免把业务错误与证书错误混在一起。

## 5. 网页绑定域名，验证公网 HTTPS

网页“域名与 HTTPS → 绑定域名”：

| 域名 | 应用 | 容器端口 | TLS |
|---|---|---|---|
| `app.YOUR_DOMAIN` | acceptance / frontend | 8080 | 正式证书 |
| `api.YOUR_DOMAIN` | acceptance / backend | 8080 | 正式证书 |

等待“代理已同步”，再点“查看证书”，应为“系统信任”且域名和时间正确。首签可能需要数分钟。“代理已同步”只代表代理配置应用成功，不代表证书已签发。

在**电脑公网链路**验证，替换真实域名，不能添加 `-k` 跳过证书验证：

```bash
curl --noproxy '*' -I http://app.YOUR_DOMAIN/
curl --noproxy '*' https://app.YOUR_DOMAIN/health
curl --noproxy '*' -X POST https://app.YOUR_DOMAIN/api/visits
curl --noproxy '*' https://api.YOUR_DOMAIN/api/visits
```

通过标准：HTTP 301/308 跳到 HTTPS；health 返回 ok；前端写入和后端读取均成功，postgres 与 redis 值一致。浏览器直接访问 HTTPS 无证书警告。

示例前端已内置 `/api` 转发，浏览器不需要直接跨域请求 backend。你自己的前端若直接请求 `api` 域名，需要后端配置实际前端 Origin 的 CORS。第一版每个完整域名绑定一个应用端口，不提供同域多路径编排。

## 6. 故障、重启和数据保留验收

只在这个测试项目执行，先记录数据库计数：

| 操作 | 预期 |
|---|---|
| 网页停止 PostgreSQL | 后端健康变为不健康/503，日志能定位依赖错误 |
| 网页启动 PostgreSQL | 后端恢复 healthy，原数据可读 |
| 网页停止 frontend，等一次协调周期 | 新 HTTPS 请求 503；启动后恢复 |
| 服务器 `sudo systemctl restart deploy-agent` | 应用、域名与数据仍在；等待协调后 HTTPS 恢复 |
| 服务器 `sudo systemctl restart deploy-proxy` | 等待路由恢复，HTTPS 信任正常、数据仍在 |
| 服务器 `sudo reboot` 后重新连接 | 四个服务 active，四应用恢复，HTTPS 与数据正常 |

重启后电脑 SSH 隧道需要重新建立。另建一个独立普通测试应用，主动停止后再重启服务器，确认它仍保持停止；不要通过停止四服务的依赖来验证停止意图后又期待四服务全部自动运行。

最后试域名停用/启用、移除。等待同步收敛后，停用或移除的域名不能再提供业务页面（可能 TLS 拒绝或 404），应用和数据应保留。新请求用于判断，旧连接可能继续完成。

自动续期：第一次拿到证书只证明首次签发成功。记录到期时间并后续观察真实续期；不要在云节点修改系统时间或临时缩短生产续期参数。本地隔离 CA 的自动续期已由专项测试覆盖，不能当作你的公网续期实测。

## 7. 可选：直接运行自动四服务自检

不想先手工配置时，可在专用测试节点运行：

```bash
sudo cloudctl test fullstack \
  --image docker.io/library/cloud-fullstack-demo:0.4-acceptance \
  --pull-policy never --keep-running
```

它自动创建随机 `stack-...` 项目、密码、四服务，验证读写、数据库故障、后端重建和跨项目隔离；会重启 Agent。输出项目名和前端地址可用于网页/HTTPS 继续测试。使用它就不要同时部署另一套 acceptance 四服务，避免 2 GiB 节点内存压力。结束后在网页停止测试项目，保留的卷/密钥/镜像不自动删除。

## 8. 常见失败与记录

```bash
sudo cloudctl status
sudo cloudctl system-logs agent
sudo cloudctl system-logs control
sudo cloudctl system-logs proxy
sudo cloudctl diagnostics --project acceptance --name backend
sudo cloudctl logs --project acceptance --name backend --tail 50
sudo cloudctl route list
free -h
df -h
```

| 现象 | 先检查 |
|---|---|
| 镜像拉取失败 | 服务器 DNS、出站、仓库访问；本机 Docker 的镜像缓存不会自动出现在服务器 |
| 示例镜像不存在 / exec format error | 标签、namespace、导入结果和 CPU 架构 |
| 数据库启动失败 | 同项目密钥名/内容、卷权限、应用日志、内存与磁盘 |
| HTTPS 502/503 | 应用健康、容器端口 8080、project 网络、监听 0.0.0.0 |
| 签发一直失败 | A/AAAA、80/443、安全组与主机防火墙、系统时间、CA 日志/限额 |
| 证书不受信任 | 是否误选 staging、旧证书、客户端根证书；不靠关闭校验解决 |

记录系统/架构、版本、时间、每一步结果、任务 ID、证书到期时间和故障日志；不要分享密码/token/私钥。安装或应用配置错误修复前不要不断重跑自检堆积项目。数据保留不等于备份，本流程不覆盖业务备份恢复或压力容量保证。

完整测试矩阵见 [验收清单](acceptance.md)，本轮实测结果见 [测试报告](test-report-0.4.md)。只有在你的实例完成上述操作后，才能把该实例标为已验收。
