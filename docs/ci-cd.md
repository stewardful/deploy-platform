# dev 测试部署与 main 正式发版

分支约定：`dev` 为默认开发分支，推送后自动更新专用测试服务器；`main` 为正式发布分支，推送后自动发布 GitHub Release。服务器升级入口仍复用现有安装器，不在 2 核 2 GiB 节点编译源码。

| 推送目标 | 自动执行 | 服务器影响 |
| --- | --- | --- |
| PR | 复用 dev 的完整检查与 amd64 安装包构建、摘要校验 | 不连接服务器，不读取 SSH 密钥 |
| dev | 测试 → 按测试机架构构建 → SSH 触发 HTTPS 下载 → 独立 systemd 升级与业务验证 | 自动更新指定测试服务器 |
| main | 检查版本 → 测试 → amd64/arm64 构建 → 固定提交的版本标签 → 完整 Release | 正式节点通过网页提示后手动升级 |

PR、dev 和 main 复用 [构建工作流](../.github/workflows/build-installers.yml)。测试未通过不会部署或公开 Release。只有 push 到对应分支或在对应分支手动运行才触发服务器部署或 Release 发布。PR 构建不读取服务器 SSH 密钥，产出 amd64 安装包并校验；arm64 仍在正式发版流程构建。

## GitHub Secrets 与 Variables

在仓库 Settings → Secrets and variables → Actions 中配置以下 **Repository secrets**，无需把凭据交给聊天或写进源码：

| Secret | 内容 |
| --- | --- |
| `TEST_SSH_PRIVATE_KEY` | 能登录测试服务器的完整 OpenSSH 私钥，包含首尾行；无口令的专用部署密钥 |
| `TEST_SSH_KNOWN_HOSTS` | 经核对的服务器 known_hosts 完整记录，不是只有 SHA256 指纹 |
| `TEST_SERVER_HOST` | 测试服务器 IP 或域名，不带用户名、协议、路径或端口；IPv6 使用不带方括号的地址 |
| `TEST_SERVER_USER` | SSH 用户，例如 `ubuntu`；需要无交互 root 管理权限 |
| `TEST_SERVER_PORT` | SSH 端口；不配置时默认 22，改用其他端口时在 Secrets 中填写 |

服务器 SSH 端口需允许 GitHub runner 连接。再配置 **Repository variables**：

| Variable | 默认值 | 说明 |
| --- | --- | --- |
| `TEST_SERVER_ARCH` | `amd64` | `uname -m` 为 x86_64 选 amd64；aarch64/arm64 选 arm64 |

`TEST_SSH_PRIVATE_KEY` 是进入服务器的 SSH 凭据，与服务器拉取私有 GitHub Release 使用的 Contents:read token 是两种凭据。dev 流程使用 Actions 自带的短期 `GITHUB_TOKEN`（`actions: read`）在 CI 端获取产物下载地址，只通过 SSH 标准输入交给服务器；不新增用户 Secret，不在服务器保存 GitHub token。下载地址只允许 HTTPS 和 GitHub 产物 CDN 主机，保存在 root 私有文件中，下载完成或失败后删除。

主机密钥应先通过腾讯云控制台或已可信连接获取指纹，再与本机扫描记录核对：

```bash
# 可信服务器控制台：读取公共主机密钥指纹
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub

# 自己电脑：收集完整记录并核对指纹；替换地址、端口
ssh-keyscan -t ed25519 -p 22 YOUR_SERVER > test_known_hosts
ssh-keygen -lf test_known_hosts
```

核对一致后，将 `test_known_hosts` 全文写入 `TEST_SSH_KNOWN_HOSTS`。工作流强制校验主机密钥，绝不在运行时自动信任扫描结果。服务器重建、更换地址或 SSH 主机密钥时，需要重新核对并更新此 Secret。

能推送 dev 的人可以通过这条流程更新测试机，应使用独立测试服务器并限制分支写权限。

## 测试服务器准备

服务器使用 Ubuntu 24.04 或 26.04 LTS/systemd，确认架构、磁盘和端口满足 [安装指南](install.md)。SSH 用户需要 `sudo -n true` 成功，并能无交互执行安装所需的 root 管理命令；root 登录也需要服务器安装 sudo。确保 Python 3、sudo 和 systemd-run 可用，部署公钥已加入该用户的 authorized_keys。

首次部署可自动调用安装器安装依赖；已有平台时使用 `--upgrade --skip-apt`。运行项目须按 [升级保障](upgrade.md) 配置 `/etc/deploy-platform/upgrade-checks.json`，缺少探针、schema/运行时不兼容、任务未排空或验证失败都使 Actions 失败，保护规则不会因 dev 环境而跳过。

同一版本号下 dev 可反复部署不同提交，按清单摘要区分包。安装器仍禁止平台版本降级，所以 dev 的版本不能低于测试机当前版本。正式节点不应作为 dev 测试目标。

## 开发和发版

日常在 dev 提交和推送即可。工作流串行处理测试部署，不取消已经运行的安装器；部署前会检查 dev 分支最新提交，过期提交跳过服务器操作。

main 发版前提高 `Cargo.toml` 的 workspace 与内部组件版本，更新 Cargo.lock 并运行检查，再把经过测试的代码合并/推送 main。无需手动打标签，工作流自动创建 `vX.Y.Z`，明确指向该次 main 事件的完整提交 SHA，完全不依赖默认分支 dev。

已使用的版本号不能指向另一个提交：新提交必须升版本。相同提交重跑可继续草稿或识别已完成发布，不覆盖公开资产。两种架构均成功且资产齐全后才公开 Release。

不再通过推送版本标签启动第二次发布。需要重跑时，在 Actions 中重跑失败任务，或选择对应分支手动运行工作流。

仓库默认分支为 dev。修复通过 PR 合并到 dev 后，自动触发测试服务器部署；PR 检查通过不代表服务器已经更新。

## SSH 中断与排错

[SSH 控制脚本](../distribution/vendor/deploy-delivery/scripts/deploy-test.sh) 在传递下载授权前完成主机预检。CI 的 [产物提供器](../distribution/vendor/deploy-delivery/scripts/artifact-request.py) 固定仓库、dev 分支、运行编号、提交、架构、产物 ID、ZIP 摘要/大小和安装包摘要/大小。开发产物只包含完整平台更新包 `.run` 与 `.sha256`，不含 containerd/nerdctl/runc/CNI/Caddy 二进制，保留 3 天，避免再下载内容重复的 `.tar.gz`；正式 Release 仍发布全部资产。

SSH 只传递少量控制数据和执行器源码，完全不使用 SCP 传包。[控制入口](../distribution/vendor/deploy-delivery/scripts/remote-control.py) 创建 root 私有阶段目录，并通过独立 systemd unit 启动 [执行器](../distribution/vendor/deploy-delivery/scripts/remote-install.py)。[下载组件](../distribution/vendor/deploy-delivery/scripts/delivery.py) 通过 HTTPS 流式下载、断点恢复，验证 ZIP 摘要，再从封闭文件清单安全解包并验证安装包摘要。校验完成后才调用原 `.run --check` 和 `.run --upgrade`，已有节点加 `--skip-apt`，不跳过兼容性、维护排空、备份、业务验证与失败回退。

下载默认最多 15 分钟，独立下载子进程有硬超时；网络错误最多 4 次尝试，接受 Range 时续传，不支持 Range 时重新写入，禁止拼接错误内容。过期地址通过 `awaiting_url` 请求 CI 刷新，最多 3 次，每次等待最多 90 秒且受总下载时限约束。CI 已断开时无法获取新授权，下载会有界失败，不开始安装。没有后台轮询服务、timer 或新的平台 API。

SSH 断开、管理端重启不影响已接受的 systemd 任务；下载完成后不再需要 CI 在线。独立工作锁覆盖下载和安装，阻止上一个运行端断线后发生重叠部署。控制文件与下载地址不进入命令参数或日志，不把仓库 token 发给服务器。

Actions 等待最多四十分钟，只读查询阶段和下载进度；必要时发送地址刷新控制请求。只在服务器确认安装提交和业务验证通过后报告成功。等待超时或取消 CI 不代表远端取消，必须检查远端状态再重跑。下载失败不会进入安装；安装失败继续使用原恢复流程，不主动终止正在执行的安装器。Actions 部署 job 自身最多 50 分钟，独立远端 unit 不随 job 结束而被杀死。

从该 Actions 运行 URL 获取 RUNID，重跑次数为 ATTEMPT，例如：

```bash
sudo systemctl status deploy-platform-ci-RUNID-ATTEMPT.service
sudo cat /var/tmp/deploy-platform-ci-RUNID-ATTEMPT/result.json
sudo tail -n 100 /var/tmp/deploy-platform-ci-RUNID-ATTEMPT/install.log
```

结果包含部署的完整提交 SHA。成功删除下载的临时安装包，失败保留以供恢复。安装器的 release 与备份按原策略保留，不自动删除业务数据；频繁测试时应检查磁盘。失败回退规则与网页升级一致，未完成的安装事务需按升级指南恢复。

## 本地验证范围

组件测试验证版本/架构、标签固定 main 提交、拒绝覆盖版本、同提交发布幂等、资产完整性、权限错误、下载摘要、执行结果和不可信 SSH 参数。真实 GitHub Actions、指定服务器连接及仓库 Secrets 仍需配置并首次推送后观察，不因本地测试通过而宣称已上线。

触发器和并发行为依据 [GitHub 工作流事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows) 与 [部署并发说明](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments)。

本地组件测试覆盖下载边界、TLS/来源限制、ZIP 路径与链接、摘要/长度、URL 刷新、续传与总超时。PR 在原生 amd64 节点额外验证真实 GitHub ZIP 摘要/大小与解包契约。[ci-ssh-smoke.sh](../distribution/tests/ci-ssh-smoke.sh) 在隔离 2 CPU / 2 GiB Ubuntu 节点使用临时密钥和仅该节点信任的 HTTPS 端点，运行真实 SSH、下载、systemd 和安装器，验证错误主机密钥、损坏包拒绝、地址过期刷新、传输中断续传、并发拒绝，以及运行端断线后继续完成升级。GitHub 元数据授权在此隔离测试中使用 fixture；真实 API 与 ZIP 格式由独立 PR 契约测试覆盖。

```bash
bash distribution/tests/ci-ssh-smoke.sh /ABS/deploy-platform-VERSION-linux-ARCH.run
```

该命令仅操作自身创建的 Docker 测试节点，安装包旁需有对应 `.sha256`；不会复用用户服务器。

## 独立交付工具与基础依赖复用

交付脚本和组件测试的权威来源是私有仓库 [stewardful/deploy-delivery](https://github.com/stewardful/deploy-delivery)。平台提交经过审查的 `distribution/vendor/deploy-delivery/` 源码快照，`deploy-delivery.lock.json` 固定完整提交 SHA 和每个文件的 SHA256；`make installer-test` 首先校验快照，再执行上游交付测试。正常 PR/dev/main 不访问另一个私有仓库，不增加 Secret。不要直接修改快照，先在辅助仓库提交并验证，再显式同步：

```bash
python3 distribution/ci/vendor-delivery.py sync --source /ABS/deploy-delivery --revision FULL_40_CHARACTER_COMMIT_SHA
make installer-test
```

两个仓库的 CI **不互相触发，不自动创建组件更新 PR，也不需要 GitHub App 或跨仓库写权限**。辅助仓库独立维护和测试交付工具；平台 dev 构建自身产物，通过 SSH 通知服务器，由服务器通过 HTTPS 下载并执行受保护的升级。当前适配器仍以固定快照随平台 CI 使用，且 CI 会传递少量执行器脚本；“交付工具预装在服务器、拥有独立安装和版本更新流程”尚未实现，不能把现有 systemd 独立执行器等同于这项完整能力。后续分离应保留产物身份校验、临时下载授权与原升级回退保护。

`dev` 使用 `PACKAGE_KIND=platform-update`：完整包含平台 Agent、控制端、Web、安装器、迁移和服务配置，但不下载或打包外部运行时二进制。`.run --check` 只校验更新包自身，不联网；成功不代表基础依赖已准备好或已安装。服务器在持有安装锁时，按清单的版本、架构和锁定摘要补齐依赖，再生成完整候选版本，通过原升级保护后才切换服务。不会在服务器编译平台源码。

已有相同依赖的完整安装版本通过全部文件校验后可直接复用。持久缓存位于 `/var/cache/deploy-platform/dependencies/`，root 私有，按组件、架构和锁清单摘要区分版本，每次复用校验文件清单和摘要；损坏缓存重建，绝不把损坏内容装进候选版本。新安装或缺失依赖时从锁定的 nerdctl/Caddy 官方 GitHub HTTPS 归档下载并验证原始归档 SHA256，提取固定组件，不读取 GitHub 私有 token。依赖下载流式进行，单个归档不超过 512 MiB、最多 3 次尝试，整个依赖准备子进程有 960 秒硬时限；失败发生在维护、数据库备份和服务切换之前。它不是后台下载服务。

首次 dev 安装需要下载上游基础依赖，仍会受 GitHub 网络速度影响；之后同版本依赖无需重复下载。这里复用的是官方已发布的归档和本地校验缓存，不额外维护一份自有基础依赖 Release。缓存不会自动删除，运维可在无安装任务时删除依赖缓存以释放空间，下次按需重建；不要删除 current/releases 或业务数据来清缓存。

`main` 和手动 `make release-build` 默认 `PACKAGE_KIND=offline`，继续生成含全部依赖的 `.run`、`.tar.gz` 和 `.sha256`。离线包无需下载运行时，但操作系统软件包和首次部署业务镜像仍需提前准备或联网。PR 同时构建、校验并在 Ubuntu 24.04/26.04 上安装更新包和离线包；还验证阻断外部 HTTPS 后仍可重复安装，以及真实 SSH/TLS 故障链路。Ubuntu 26.04 还验证离线安装 → 同提交更新包的升级流程，部署真实前后端、PostgreSQL/Redis 和隔离 CA 的 HTTPS，确认数据保留及注入业务故障后的回退；同提交验收不等同于跨历史版本迁移或公网证书验收。Rust crates、Go packages 和前端组件继续编译进平台程序，不拆成每次独立发布的资产。运行时版本变化仍受现有升级兼容性限制，不能通过缓存机制绕过计划迁移要求。

隔离 systemd-in-Docker 节点统一通过 `distribution/tests/node-exec.sh` 执行命令：将测试命令移入 `init.scope`，通过 systemd 恢复资源控制器，避免 Docker exec 将命令留在根 cgroup、关闭子控制器后出现 `pids.max` 缺失。入口拒绝非测试节点，保留参数与标准输入；这不是生产安装配置或资源限制的放宽。`cloudctl smoke` 现在在首次运行和 Agent 恢复后都读取实际 cgroup v2 的 CPU/内存/PID 限制，避免仅检查容器配置而误报资源约束通过。
