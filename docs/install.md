# 腾讯云 Ubuntu 一键安装

第一版面向 **Ubuntu 24.04 / 26.04 LTS、systemd、cgroup v2、2 核 2 GiB**，支持 amd64 / arm64。目标节点安装预编译产物，Web 由 Go 控制端直接提供，不需要 Node、Go、Rust、Docker 或 Compose。Agent、控制端与运行时分别使用 systemd，默认开机启动。

## 安装和打开网页

在可信构建机生成安装包，或从自己的 CI 下载对应架构产物。服务器 `uname -m` 为 `x86_64` 选 `amd64`，为 `aarch64` 选 `arm64`。建议为镜像和版本备份留至少 10 GiB 可用磁盘；安装器最低检查 2 GiB。镜像仓库和 Ubuntu 软件源需要能访问。包已包含运行时，首次部署仍要拉取应用镜像。

把 `deploy-platform-0.5.3-linux-amd64.run` 和同名 `.sha256` 上传服务器。下面以 amd64 为例，ARM 服务器替换文件名中的架构：

```bash
# 在上传目录，检查可信来源提供的摘要；只上传 .run 时使用下面的完整性自检
sha256sum --check --ignore-missing deploy-platform-0.5.3-linux-amd64.sha256
bash deploy-platform-0.5.3-linux-amd64.run --check

# 一条命令安装 Web、控制端、Agent、独立 containerd / nerdctl / runc / CNI
sudo bash deploy-platform-0.5.3-linux-amd64.run

sudo cloudctl status
sudo cloudctl token
```

`--check` 需要 Python 3，只解包到临时目录并验证，不修改服务。正常安装会在缺少 Python 时先通过 apt 安装它，再检查全部文件。安装器不会打印登录 token；最后一条命令仅供管理员主动读取。摘要与包应通过可信渠道获取，内嵌校验不等同于发布者签名。

在自己的电脑建立隧道，`ubuntu` 换成实际 SSH 用户：

```bash
ssh -N -L 8088:127.0.0.1:8088 ubuntu@服务器公网IP
```

打开 `http://127.0.0.1:8088/`，输入 token，可部署镜像、查看应用和任务、启停项目。腾讯云安全组只需保留管理员的 SSH 访问；默认无需开放 8088。安装器不修改安全组、UFW 或现有防火墙策略。

管理入口默认仅绑定服务器回环地址，通过 SSH 隧道使用。0.4 已提供应用域名绑定、反向代理与自动 HTTPS，配置见 [域名与 HTTPS](ingress.md)；这不改变管理入口的私有访问方式。

## 部署第一个普通项目

网页点击“部署应用”，填写项目名、应用名和已构建的镜像。可用 Nginx 示例：

- 镜像：`docker.io/nginxinc/nginx-unprivileged:stable-alpine`。
- 资源：128 MiB、250 millicpu、64 个进程。
- 网络：bridge；容器端口 8080，宿主端口 18080。

等待任务成功。任务成功代表启动观察通过，业务 HTTP 仍应主动访问检查。在电脑另建业务隧道：

```bash
ssh -N -L 18080:127.0.0.1:18080 ubuntu@服务器公网IP
```

访问 `http://127.0.0.1:18080/`。显式发布的应用端口固定绑定服务器 `127.0.0.1`；公网业务使用 project 网络及 [域名绑定](ingress.md)，不必映射宿主端口。生产应用建议使用自己验证过的固定镜像 digest。

也可执行完整的真实部署自检：

```bash
sudo cloudctl test smoke
# 或首次安装时添加 --smoke-test
```

自检会拉取示例镜像，部署独立 `smoke-*` 项目，检查 HTTP、CPU/内存/PID 限制和请求幂等性，停止测试容器并重启 Agent 验证自动恢复，最后通过 API 保存停止意图并删除自己的测试容器。它会短暂重启 Agent；应在空闲时执行。已停止的测试项目元数据与镜像缓存保留，网页仍可看到项目。失败时按输出的项目名检查，不会执行全局清理。

## 安装内容和组件边界

| 路径 / 服务 | 用途 |
| --- | --- |
| `/opt/deploy-platform/releases/<版本>-<摘要>` | 不可变版本，包含三个应用二进制、Web、运行时与安装组件 |
| `/opt/deploy-platform/current` | 当前版本的原子切换链接 |
| `/opt/deploy-platform/nerdctl` | 固定独立运行时配置的适配脚本 |
| `/etc/deploy-platform/` | containerd、nerdctl、CNI、安装记录、私有 token |
| `/var/lib/deploy-platform/agent.sqlite` | 应用配置、期望状态、持久化任务；升级前在线备份 API 生成一致快照 |
| `/var/lib/deploy-platform/containerd/` | 专属镜像、快照和容器数据 |
| `/var/lib/deploy-platform/nerdctl/` | 专属 nerdctl 数据和应用容器日志 |
| `/var/lib/deploy-platform/backups/` | 每次激活前的配置、安装记录与 SQLite 备份 |
| `deploy-containerd.service` | 专属 containerd，与系统已有运行时使用不同 socket 和数据目录 |
| `deploy-agent.service` | Rust Agent，串行部署、恢复、私有 Unix Socket API |
| `deploy-control.service` | Go HTTP API + 静态 Web，连接本地 Agent |

默认 namespace 是 `deploy-platform`。专属 socket 为 `/run/deploy-platform-containerd/containerd.sock`，Agent socket 为 `/run/deploy-platform/agent.sock`。已安装 Docker 的机器也不会替换系统 `/usr/bin/containerd` 或现有 unit，但仍共享主机网络、内核和资源，需检查实际环境兼容性。

运行时取自 [nerdctl 官方 v2.4.1 发布包](https://github.com/containerd/nerdctl/releases/tag/v2.4.1)，固定 nerdctl/containerd 2.4.1、runc 1.5.2、CNI 1.9.1 及官方 SHA256。仅打包运行所需二进制和六个 CNI 插件，保留上游文档/许可，不运行 BuildKit、Kubernetes、CRI 或额外数据库。变更版本从 `distribution/release/runtime.lock.json` 统一审查，不能只改下载地址。

独立 bridge 名为 `cloud0`，默认从三个私有 /24 网段中选择不与当前路由冲突的网段。均冲突则明确拒绝，可首次安装时用 `--bridge-cidr 10.120.250.0/24` 指定经过检查的私有网段。安装后 CIDR 不支持原地变更。旧 bridge 模式仍共享。0.3 新增 project 网络，为各项目创建独立 CNI bridge 和防火墙策略，详见 [完整部署指南](fullstack.md)。

安装会写入专属 modules-load/sysctl 文件，启用 IPv4 转发及可用的 bridge iptables 设置；不会清空规则、创建 swap、删除已有容器、镜像或卷。

## 适配 2 核 2 GiB

目标机不做源码或前端构建；控制端不需要 Node 常驻；Agent 单 worker、低权重执行，恢复轮询 10 秒并带退避。控制端 `GOMEMLIMIT=64MiB`、systemd `MemoryHigh=96M`，Agent `MemoryHigh=192M`；这些是软限制，不是整个平台内存上限。containerd 不设置容易破坏运行时的硬内存上限。

新建容器启用 json-file 日志轮转，每份 10 MiB、保留 3 份；升级不会更改已经创建的容器设置。恢复服务使用 journal，宿主 journald 的磁盘配额由管理员管理。应用镜像、旧版本、备份不自动清理，磁盘使用可通过系统工具与 `cloudctl runtime system df` 检查。

建议先运行一个 128–256 MiB 普通项目，再按实际余量增加项目。2 GiB 不适合同时运行大量数据库或本地大模型。当前已支持应用专属数据卷与项目密钥文件，可部署数据库和 Redis，使用见 [有状态应用指南](stateful.md)。AI 镜像可以使用同样的数据卷和密钥引用；项目网络隔离已提供，GPU 调度尚未实现。

## 重复安装、升级和失败恢复

0.5.2 起增加升级前兼容性检查、维护排空、隔离数据库预演和已有项目业务验证。**有运行中项目时，先配置只读业务探针**，缺少覆盖会拒绝升级；流程、配置和失败处理见 [升级保障](upgrade.md)。重装同一包也执行这些保护。

相同包可重复运行，保留 token、数据、应用和停止意图；会短暂停止控制端/Agent并重新检查。不同文件摘要的版本必须明确使用 `--upgrade`：

```bash
sudo bash deploy-platform-新版本-linux-amd64.run --upgrade
```

激活前保存配置和 SQLite 一致备份，服务使用 release 链接切换。安装器使用节点锁防止并行安装，私有事务记录让下次运行先恢复被中断的激活。失败时恢复前一个版本的文件、配置和原有活动服务；数据库、应用容器和镜像不自动回退。首次失败会留下受管理的失败记录，修复原因后可重跑。不要手工删除事务记录后强行继续。

当前只支持运行时版本完全相同的应用层升级；containerd/runc/CNI 升级需另行设计迁移。不能据此承诺跨数据库 schema 的自动降级；若新 Agent 已做不兼容数据库迁移，需要管理员审查备份并制定恢复方案。apt 安装的软件、已经加载的内核模块和实时 sysctl 不自动撤销。旧 release/备份保留供诊断，不会自动删除。

如果已存在非本安装器管理的同名 unit、配置、cloudctl 或数据库，会拒绝覆盖；若管理文件被手工修改，也会要求先保存并协调修改。首次安装前应使用干净节点，或先单独迁移旧版手工部署。安装完成后 `--smoke-test` 失败只表示业务自检未通过，不撤销已经通过健康检查的安装。

## 运维命令

```bash
sudo cloudctl status
sudo cloudctl system-logs agent
sudo cloudctl system-logs control
sudo cloudctl system-logs runtime
sudo cloudctl apps
sudo cloudctl runtime ps -a
sudo systemctl restart deploy-agent
sudo systemctl status deploy-containerd deploy-agent deploy-control
```

`cloudctl runtime` 是管理员排查入口。修改受 Agent 管理的运行容器可能触发自动恢复；正常启停应从网页/API完成。不要将 root Unix Socket 或 token 暴露给不受信任用户。

## 构建和复用

源码目录：

```text
distribution/release/          固定运行时清单、Linux 构建与纯打包组件
distribution/installer/lib/    归档校验、主机检查、网络选择、配置生成、快照/恢复
distribution/installer/systemd/ 独立运行时、Agent、控制端服务
distribution/installer/        自解压入口、安装编排、cloudctl、节点自检
distribution/tests/            Ubuntu systemd 测试镜像与可选集成测试
tests/installer/           纯 Python 组件回归测试
```

这些部署组件不进入 Rust/Go 领域库。独立 Agent 仍可通过自己的配置接入其他运行时安装方式；SDK、Web 静态适配器不依赖安装器。`distribution/manual/systemd/` 模板仅用于已有运行时的手动部署，与一键安装使用的专属运行时模板分开，不要混用路径。

在原生 Linux 构建机安装 Rust 1.96、Go 1.25、Node 24、Python 3.11+、C 编译器、curl 后执行：

```bash
make installer-test
VERSION=0.5.3 make release-build
```

生成 `target/dist/*.run`、`.tar.gz`、`.sha256`。`package.py` 也可以接收已构建的 `--bin-dir`、`--web-dir` 和 `--runtime-archive`，会检查 ELF 架构和每个文件的摘要。`Dockerfile.agent` 可从其他系统构建 Linux Agent，Go 可交叉编译，Web 产物与架构无关。

推送 main 触发两种架构构建、校验和 GitHub Release 发布；dev 推送发布公开预发布包，服务器通过Webhook 通知和本地策略拉取更新，见 [CI/CD](ci-cd.md)。正式节点仍通过 [网页手动升级](updates.md)，旧版需先安装包含更新器的版本。

Ubuntu 26.04 的适配与本地安装、升级验收入口见 [Ubuntu 兼容性验证](ubuntu-compatibility.md)。


## 统一 cloudctl 首次安装

公开仓库的 dev 构建发布到 GitHub 预发布 Release，服务器无需 GitHub token、GitHub CLI、Git、Rust、Go、Node 或额外交付工具。需要 Python 3、curl、systemd、cgroup v2，以及访问 GitHub/Release CDN 和 Ubuntu 软件源的网络。

首次在服务器准备命令（须等本改动合并、新的发布工作流成功发布第一份预发布包后执行）：

```bash
mkdir -p ~/deploy-platform-bootstrap
cd ~/deploy-platform-bootstrap
curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --connect-timeout 20 --max-time 120 \
  -o cloudctl https://github.com/stewardful/deploy-platform/releases/download/dev-latest/cloudctl
curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --connect-timeout 20 --max-time 120 \
  -o cloudctl.sha256 https://github.com/stewardful/deploy-platform/releases/download/dev-latest/cloudctl.sha256
sha256sum -c cloudctl.sha256
```

摘要必须显示 `cloudctl: OK`，失败时不要执行；发布切换期间两个小文件可能属于不同构建，可重新下载这两个文件后重试校验。校验通过后，一条命令完成首次安装：

```bash
sudo /usr/bin/python3 -I -B ./cloudctl install --channel dev
```

默认读取公开 `dev-latest/dev.json`，随后固定到 `dev-RUN_ID/dev.json` 和该构建的安装包。首次安装自动识别 amd64/arm64，检查来源、版本、摘要、大小和内嵌提交记录。`--run-id ID` 可指定已经发布的构建，无需登录 GitHub；不存在或被删除的构建明确失败，不回退到其他版本。`--repository owner/repo` 可选择使用同一发布契约的公开仓库，默认 stewardful/deploy-platform。

正式版：`sudo /usr/bin/python3 -I -B ./cloudctl install --channel stable --version X.Y.Z`；省略 --version 选择最新正式 Release。私有正式 Release 可额外传 `--token-file /绝对路径/github.token`，文件权限必须 0600，凭据需要 Contents:read；公开 dev 路径不读取该 token。没有新增私有 dev Actions 下载兼容路径。

已有本地安装包时：

```bash
sudo /usr/bin/python3 -I -B ./cloudctl install --file /绝对路径/deploy-platform-0.5.3-linux-amd64.run
```

install 只用于首次安装，已有平台会明确拒绝，不重置数据。命令默认等待完成；`--no-wait` 仅提交任务。首次安装在独立 systemd 任务中运行，SSH 断开不取消；使用 `sudo cloudctl update status` 和 `sudo cloudctl system-logs install` 查询。若命令等待超时，先查询任务，不重复安装。服务器重启不自动重试中断的安装。

安装后固定入口为 `/usr/local/bin/cloudctl`，后续更新一条命令：

```bash
sudo cloudctl upgrade-dev --yes
```

该命令提交更新，成功受理不代表安装已完成；用 `sudo cloudctl update status` 查询，`phase=succeeded` 才代表完成。不加 --yes 只预览。安装器保留兼容性检查、维护排空、备份、业务验证和失败回退。

首次下载器与平台内 CLI 共用源码和安装逻辑；bootstrap 缓存位于 `/opt/deploy-platform/bootstrap`，供安装任务固定代码使用，不是另一个产品或常驻服务。首次安装失败保留 CLI 入口用于查询日志。

## 构建产物位置

- Actions Artifacts：`cloudctl`（14 天）、`deploy-platform-linux-amd64/arm64`（dev 更新包 3 天）；PR 另有完整离线验证包（3 天）。这些是 CI 中间产物，不是服务器手动安装的数据源。
- GitHub Releases：每次成功 dev 构建发布 `dev-RUN_ID` 预发布，包含 cloudctl、校验文件、两架构平台更新包和 dev.json；`dev-latest` 只放最新发现清单和 cloudctl 两个小文件。预发布不自动过期，不覆盖固定构建资产，暂无自动清理；删除后无法再用该构建编号安装。
- 正式 `vX.Y.Z` Release：cloudctl、两种架构完整离线包及摘要。dev 只打平台更新包，基础运行时按锁定版本下载或复用。
- 安装后的服务器：`/opt/deploy-platform/releases/` 存放不可变版本，`current` 指向当前版本；配置在 `/etc/deploy-platform/`，业务数据在 `/var/lib/deploy-platform/`。

自动通知升级仅通过现有 Go 控制端接收已验签 Webhook，dev 开关默认关闭，正式版只提示。使用 `sudo cloudctl update policy --dev-auto-upgrade on` 开启 dev 自动升级；详见 [CI/CD](ci-cd.md)。没有 SSH 自动部署或 cloudctl delivery 入口。
