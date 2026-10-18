# 0.4.1 测试与修复记录

执行日期：2026-10-18。开发环境 macOS ARM64；真实运行时使用独立 Ubuntu 24.04 ARM64/systemd/cgroup v2 节点，限制 2 CPU / 2 GiB / 512 PID，无主机目录挂载。未连接用户腾讯云服务器。

## 本轮发现并修复

### 自检后无法再次安装/升级

0.4.0 运行有状态或四服务自检时，Python 导入节点程序会在已安装的发布目录生成 `__pycache__/smoke...pyc` 和 `examples/fullstack/__pycache__/generate...pyc`。随后重复安装会因为严格文件清单中没有这两个文件而报 `release inventory mismatch`。本轮真实节点复现；失败发生在切换服务前，已有应用仍正常。

0.4.1 的安装、自解压和 cloudctl 入口禁止写字节码；独立自检脚本也在导入本地组件前禁止缓存。保留严格文件清单检查，不忽略额外文件。新增真实导入回归用例，并在 Ubuntu 集成入口补上“自检后验证全量清单、再次安装、再次比较令牌”的检查。

实测从带这些缓存的 0.4.0 节点升级到 0.4.1 成功；再运行真实 PostgreSQL/Redis 自检、fullstack 帮助入口后，同版本重装及清单校验成功。升级/重装前后比较管理令牌、所有密钥文件、卷元数据摘要，以及域名配置/版本和 HTTPS 读回的 SQL/Redis 数据，均保持一致。摘要在节点内比较，不输出秘密。

另修正 Go 集成测试文件格式，使其满足现有 CI 的 gofmt 检查。

## 测试矩阵

| 层次 | 本轮执行内容 | 结果 |
|---|---|---|
| Rust | workspace 单元/集成/文档测试 | 69 通过；默认忽略的专项另行执行 |
| Rust 质量 | Clippy `-D warnings`、fmt、agent-http 独立 client/server feature | 通过 |
| Linux 专项 | 默认忽略的 `linux_apply`，真实 nerdctl/containerd/Unix API、进程重启恢复、停止意图 | 1 通过 |
| 最低 Rust | Linux Rust 1.85 `check --workspace --all-targets --locked` | 通过 |
| Go | 无缓存 race、真实 Rust Socket 集成、vet、gofmt、构建 | 通过 |
| 最低 Go | Go 1.23.12 测试与构建 | 通过 |
| 独立示例 | Go 1.26.9 vet，Linux ARM64/AMD64 静态构建 | 通过 |
| Web | TypeScript 检查、8 项 SDK 测试、格式、生产构建 | 通过 |
| 浏览器 | 真实 Go/Rust API + 运行时替身：应用任务、密钥模板、域名管理 | 3 通过 |
| 真实网页链路 | 浏览器 → 节点 Go/Rust/Caddy，绑定/证书检查/停启用/删除 | 通过 |
| 安装组件 | 归档、摘要、架构、配置、事务恢复、字节码回归 | 9 通过 |
| Shell | 安装脚本语法、ShellCheck | 通过 |
| 真实安装 | 新装、重复安装、令牌保留、自检后再安装 | 通过 |
| 普通应用 | 拉镜像、HTTP、资源限制、幂等、Agent 恢复 | 通过 |
| 有状态应用 | PostgreSQL/Redis 认证读写、容器重建、卷/密钥保留、停止意图 | 通过 |
| 四服务 | 前端→后端→PostgreSQL/Redis、数据库故障与健康/日志、后端重建、跨项目名/IP 隔离 | 通过 |
| HTTPS | 独立本地 ACME CA 的真实 HTTP-01、信任链/主机名验证、自动续期、HTTP 跳转、SNI/Host 421、旧版本编辑 409、503/恢复 | 通过 |
| 安装包 | 两架构二进制/镜像架构与标签、安装包全量清单及 SHA256、与最终 Web 构建逐文件比对 | 通过 |

当前 Go race/Socket 测试使用 `-count=1` 重新执行，未把命中缓存的旧结果当成本轮结果。macOS Rust Socket 用例需正常创建 Unix Socket 的权限；默认沙箱拒绝后使用正常权限重跑通过。Rust 1.85 在当前 macOS SDK 遇到链接器不兼容，最低版本检查改在 Linux 执行并通过。

多轮保留测试数据后，临时节点的 1 GiB tmpfs 被填满，最后一次 Linux 镜像解压报 `no space left on device`。定位后仅把该临时数据盘扩大到 1.5 GiB，再执行专项；节点仍限制 2 CPU / 2 GiB。全新节点的标准安装/数据库自检使用原 1 GiB tmpfs 已通过。生产节点使用真实磁盘，手册要求预留容量并检查镜像/数据/日志占用。

## 复跑入口

开发机依照 [验收清单](acceptance.md#5-源码回归开发机ci不在-2-gib-服务器编译)。Linux 构建/最低版本检查可使用：

```bash
docker build --target msrv -f distribution/tests/Dockerfile.rust -t cloud-rust-msrv .
```

此入口先执行 Linux 普通 Rust 测试，再做最低版本检查；默认忽略的运行时用例需在明确准备的 Linux 节点另行运行，不会在构建时自动操作宿主运行时。

完整安装入口，要求 Docker 后端为 ARM64、允许 privileged/systemd/cgroup v2；内部节点为可删除测试环境：

```bash
STATEFUL=1 bash distribution/tests/ubuntu-smoke.sh "$PWD/target/dist/deploy-platform-0.4.1-linux-arm64.run"
```

公网用户节点按 [腾讯云完整操作手册](tencent-ubuntu-walkthrough.md) 验收。`distribution/tests/https-runtime.py` 仅用于明确标记、配置本地 ACME CA 的可删除节点，禁止直接拿到正式服务器运行。

## 边界

- 真实安装与容器运行验收在 ARM64 上完成；AMD64 为编译、架构/归档/摘要校验，不能称为 AMD64 云主机实测。
- TLS 使用独立本地 CA，公钥只在本次临时容器中受信；没有向开发电脑添加信任。生产安装默认仍为公网 Let's Encrypt，不包含测试 CA/hosts/覆盖配置。
- 续期验收仅在可删除节点加速检查参数，最终恢复生产默认。不能把本地签发/续期说成腾讯云上的公网 Let's Encrypt 验收。
- 没有验证用户服务器的安全组/DNS、整机重启/断电、业务备份恢复、长期压力容量或 GPU；操作手册安排了需要用户实际执行的整机重启与公网验证。
- 修复只涉及安装入口、测试、版本与文档；Go/Rust 容器部署逻辑和 Caddy 生产策略没有因测试而放宽。

本轮日志位于工作区 `target/current-*.log`，运行时验收与证书参数只存在于本任务临时节点中。
