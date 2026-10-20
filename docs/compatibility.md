# 兼容性与验证边界

| 项目 | 当前状态 |
| --- | --- |
| 运行目标 | Ubuntu 24.04 LTS、Linux cgroup v2 |
| 开发平台 | Rust 模型、参数与模拟测试可在 macOS/Linux 运行 |
| 最低 Rust | workspace 最低 1.85，已在 Linux 检查；发布使用 1.96 |
| nerdctl/containerd/runc 版本 | 固定安装包版本已在隔离 Ubuntu ARM64 环境验证，尚未连接真实腾讯云节点 |
| CLI apply / SQLite | 已实现，常规及显式 Linux 专项均通过；范围见测试报告 |
| daemon | 模拟/实际 Linux 进程与 systemd 验收通过；真实云服务器整机重启仍待验收 |
| Go 控制端 | 单节点 HTTP API + 独立 Unix SDK，标准库；最低 Go 1.23 已检查，隔离 Linux 链路已验证 |
| Web | React 18 / TypeScript / TDesign，Vite 静态构建，需现代浏览器；构建需 Node >=22.12，生产无需 Node |
| API | HTTP/1.1 + JSON Unix Socket v1；真实 Socket、替身与实际 Linux 运行时均有验收 |
| 状态 schema | v3，直接初始化；拒绝 v1/v2 与未知结构，升级前备份 |
| 网络 | 默认 none；bridge 共享，project 提供项目网络/服务名/防火墙隔离；发布端口固定回环 |
| 域名/HTTPS | 受限 Caddy 代理、显式域名、自动证书；本地隔离 CA 已验收，用户公网需另验 |
| 密钥/数据卷 | 0.2.0 支持项目密钥只读文件、应用专属目录卷；无自动删除/轮换/业务备份 |

运行时契约参考 [nerdctl 命令文档](https://github.com/containerd/nerdctl/blob/main/docs/command-reference.md)。文档 main 分支会变化，实际交付必须记录并固定经过集成测试的二进制版本和校验值。

真实 Linux 验收应覆盖：私有仓库凭证（后续实现）、镜像架构不匹配、运行时版本差异、退出状态、输出洪泛、超时后的实际状态、资源限制、日志恢复、网络隔离、磁盘不足和主机重启。假执行器测试只验证适配器逻辑；已经实际执行的子集见 [测试报告](test-report-0.4.md)，不能把本地容器验证等同真实云主机。


SQLite 使用 bundled 模式。原先纯 Rust 的 Linux 交叉检查需要升级为带目标 C 工具链的检查，或在 Ubuntu 原生编译。macOS 上通过的常规测试不覆盖真实 CNI/端口转发和 cgroup 限制。Linux 实机测试见 apps/agent/tests/linux_apply.rs，默认忽略，须显式运行。

## 一键安装运行时

Ubuntu 安装器使用独立 socket、数据目录和 namespace，固定运行时版本与官方摘要见 `distribution/release/runtime.lock.json`。支持 Ubuntu 24.04 / cgroup v2 / systemd 的 amd64 与 arm64；不自动改造 Ubuntu 22.04、cgroup v1 或既有手工安装。

真实 nerdctl 2.4.1 的 Docker 兼容 inspect 在没有 containerd task 时返回空 `State.Status`。适配器仅在 Running/Paused/Restarting 明确为 false 且 Pid=0 时视为 created；字段缺失、未知文本和不一致状态继续返回 unknown。这个判定不跳过容器归属和配置指纹校验。[上游实现](https://github.com/containerd/nerdctl/blob/v2.4.1/pkg/inspecttypes/dockercompat/dockercompat.go)

containerd 使用 umask 0022，nerdctl 适配脚本也显式设置 022，让镜像内非 root 用户可以访问生成的 rootfs/配置；外层运行时和 Agent 数据目录仍为 root 私有，Agent 数据库继续使用私有权限。创建容器时限制 json-file 日志轮转为 10 MiB × 3；已经部署的容器不会因升级被替换或更改日志策略。

安装、备份、恢复边界与上云验收见 [安装指南](install.md)。
