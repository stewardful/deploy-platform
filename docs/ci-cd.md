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

**首次只使用 cloudctl**：从本仓库成功 CI 的 `cloudctl` 产物获取命令和摘要，在服务器校验后执行 `sudo /usr/bin/python3 -I -B ./cloudctl install --channel dev --token-file /绝对路径/github.token`，或使用 `--file /绝对路径/平台.run` 离线安装。安装完成后用 `sudo cloudctl delivery preflight amd64` 检查节点。命令会输出当前仓库、架构与 `phase=ready`；CI 只向已经安装的平台发指令，不上传执行代码。详见 [首次安装](install.md#统一-cloudctl-首次安装)。

首次部署可自动调用安装器安装依赖；已有平台时使用 `--upgrade --skip-apt`。运行项目须按 [升级保障](upgrade.md) 配置 `/etc/deploy-platform/upgrade-checks.json`，缺少探针、schema/运行时不兼容、任务未排空或验证失败都使 Actions 失败，保护规则不会因 dev 环境而跳过。

同一版本号下 dev 可反复部署不同提交，按清单摘要区分包。安装器仍禁止平台版本降级，所以 dev 的版本不能低于测试机当前版本。正式节点不应作为 dev 测试目标。

## 开发和发版

日常在 dev 提交和推送即可。工作流串行处理测试部署，不取消已经运行的安装器；部署前会检查 dev 分支最新提交，过期提交跳过服务器操作。

main 发版前提高 `Cargo.toml` 的 workspace 与内部组件版本，更新 Cargo.lock 并运行检查，再把经过测试的代码合并/推送 main。无需手动打标签，工作流自动创建 `vX.Y.Z`，明确指向该次 main 事件的完整提交 SHA，完全不依赖默认分支 dev。

已使用的版本号不能指向另一个提交：新提交必须升版本。相同提交重跑可继续草稿或识别已完成发布，不覆盖公开资产。两种架构均成功且资产齐全后才公开 Release。

不再通过推送版本标签启动第二次发布。需要重跑时，在 Actions 中重跑失败任务，或选择对应分支手动运行工作流。

仓库默认分支为 dev。修复通过 PR 合并到 dev 后，自动触发测试服务器部署；PR 检查通过不代表服务器已经更新。

## SSH 中断与排错

[SSH 控制脚本](../distribution/ci/deploy-test.sh) 在传递下载授权前完成主机预检。CI 的 [产物提供器](../distribution/ci/artifact-request.py) 固定仓库、dev 分支、运行编号、提交、架构、产物 ID、ZIP 摘要/大小和安装包摘要/大小。开发产物只包含完整平台更新包 `.run` 与 `.sha256`，不含 containerd/nerdctl/runc/CNI/Caddy 二进制，保留 3 天，避免再下载内容重复的 `.tar.gz`；正式 Release 仍发布全部资产。

项目 CI 的 SSH 只调用预装命令并传递有限的 JSON 元数据/临时下载授权，不上传执行代码，也不通过 SCP 传平台包。[已安装的控制入口](../distribution/installer/lib/ci_control.py) 校验协议与绑定仓库，创建 root 私有任务目录，并通过独立 systemd unit、固定的不可变版本路径启动 [执行器](../distribution/installer/lib/ci_worker.py)。[下载组件](../distribution/installer/lib/delivery.py) 通过 HTTPS 流式下载、断点恢复，验证 ZIP 摘要，再从封闭文件清单安全解包并验证安装包摘要。校验完成后才调用原 `.run --check` 和 `.run --upgrade`，已有节点加 `--skip-apt`，不跳过兼容性、维护排空、备份、业务验证与失败回退。

下载默认最多 15 分钟，独立下载子进程有硬超时；网络错误最多 4 次尝试，接受 Range 时续传，不支持 Range 时重新写入，禁止拼接错误内容。过期地址通过 `awaiting_url` 请求 CI 刷新，最多 3 次，每次等待最多 90 秒且受总下载时限约束。CI 已断开时无法获取新授权，下载会有界失败，不开始安装。没有后台轮询服务、timer 或新的平台 API。

SSH 断开、管理端重启不影响已接受的 systemd 任务；服务器重启会中断瞬态交付任务，须检查安装事务后用新 attempt 重试。下载完成后不再需要 CI 在线。独立工作锁覆盖下载和安装，阻止上一个运行端断线后发生重叠部署。控制文件与下载地址不进入命令参数或日志，不把仓库 token 发给服务器。

Actions 等待最多四十分钟，只读查询阶段和下载进度；必要时发送地址刷新控制请求。只在服务器确认安装提交和业务验证通过后报告成功。等待超时或取消 CI 不代表远端取消，必须检查远端状态再重跑。下载失败不会进入安装；安装失败继续使用原恢复流程，不主动终止正在执行的安装器。Actions 部署 job 自身最多 50 分钟，独立远端 unit 不随 job 结束而被杀死。

从该 Actions 运行 URL 获取 RUNID，重跑次数为 ATTEMPT，例如：

```bash
sudo systemctl status deploy-platform-ci-RUNID-ATTEMPT.service
sudo /usr/local/bin/cloudctl delivery status RUNID ATTEMPT
sudo /usr/local/bin/cloudctl delivery logs RUNID ATTEMPT
```

结果包含部署的完整提交 SHA。成功删除下载的临时安装包，失败保留以供恢复。安装器的 release 与备份按原策略保留，不自动删除业务数据；频繁测试时应检查磁盘。失败回退规则与网页升级一致，未完成的安装事务需按升级指南恢复。

## 本地验证范围

组件测试验证版本/架构、标签固定 main 提交、拒绝覆盖版本、同提交发布幂等、资产完整性、权限错误、下载摘要、执行结果和不可信 SSH 参数。真实 GitHub Actions、指定服务器连接及仓库 Secrets 仍需配置并首次推送后观察，不因本地测试通过而宣称已上线。

触发器和并发行为依据 [GitHub 工作流事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows) 与 [部署并发说明](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments)。

本地组件测试覆盖下载边界、TLS/来源限制、ZIP 路径与链接、摘要/长度、URL 刷新、续传与总超时。PR 在原生 amd64 节点额外验证真实 GitHub ZIP 摘要/大小与解包契约。[ci-ssh-smoke.sh](../distribution/tests/ci-ssh-smoke.sh) 在隔离 2 CPU / 2 GiB Ubuntu 节点使用临时密钥和仅该节点信任的 HTTPS 端点，运行真实 SSH、下载、systemd 和安装器，先验证未安装平台时拒绝 CI 部署、cloudctl 首次安装和仅数据传输，再验证错误主机密钥、损坏包拒绝、地址过期刷新、传输中断续传、并发拒绝，以及运行端断线后继续完成升级。GitHub 元数据授权在此隔离测试中使用 fixture；真实 API 与 ZIP 格式由独立 PR 契约测试覆盖。

```bash
bash distribution/tests/ci-ssh-smoke.sh /ABS/deploy-platform-VERSION-linux-ARCH.run
```

该命令仅操作自身创建的 Docker 测试节点，安装包旁需有对应 `.sha256`；不会复用用户服务器。


## 单仓库维护

CLI、CI 控制端、下载组件、安装器和测试全部在本仓库维护。`distribution/cli` 构建通用 cloudctl；`distribution/ci` 负责 runner 端指令；`distribution/installer/lib` 负责节点逻辑。没有独立工具安装包、跨仓库同步、GitHub App 或额外 CI Secret。正式 Release 同时分发 cloudctl、其校验文件及两种架构的完整离线平台包。
