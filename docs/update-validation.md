# 0.5.2 版本发现与网页升级验收

执行日期：2026-10-19。代码在本地验证，未推送版本标签、未创建真实 GitHub Release，也未连接用户腾讯云服务器。

| 检查 | 结果 |
| --- | --- |
| Rust workspace | 76 项通过，1 项独立运行时专项默认忽略；Agent 组件结构保持不变 |
| Go | 全模块 race、vet、真实 Rust Unix Socket 集成通过；新增版本/架构/凭据/陈旧候选/并发只接受一次及 HTTP 鉴权验证 |
| TypeScript | SDK 9 项、类型、格式、生产构建通过 |
| 浏览器 | 已有 Chromium 运行 4 项通过，覆盖新版本提示、确认前不提交、确认后排队、刷新不重复提交 |
| 安装器 | 42 项通过，覆盖已有升级保护及新下载器的身份、流式摘要、大小、磁盘、跳转脱敏、中断不重试 |
| 发布工作流 | 本地 YAML 解析及任务依赖检查通过；尚未运行远程发布任务 |
| 安装包 | 原生 ARM64/交叉 amd64 Rust 与 Go Linux 二进制，包自检及两种架构 SHA256 通过；打包的更新模块与源码一致 |
| 静态质量 | Rust fmt/Clippy/组件特性、Go 格式/vet、Web 类型/格式、Bash 语法、ShellCheck、文档链接及 diff 空白检查通过 |

## 隔离 Ubuntu 完整链路

可复用入口：[upgrade-smoke.sh](../distribution/tests/upgrade-smoke.sh)，设置 `CLOUD_TEST_WEB_UPDATE=1`。GitHub HTTPS 测试端点见 [update-delivery.py](../distribution/tests/update-delivery.py)。

本次使用私有 0.5.1 启动测试包：保留已验证的 0.5.1 Agent 二进制，加入更新交付功能，作为可升级旧节点；这不是公开的历史 Release。目标是本次 0.5.2 ARM64 安装包。

节点限制 2 CPU / 2 GiB / 512 PID，真实 systemd/containerd/nerdctl，部署前端、后端、PostgreSQL 17 和 Redis 7.4，使用仅测试节点信任的隔离 ACME CA 签发 HTTPS：

1. 缺少业务验证探针时，安装器在停止服务前拒绝升级，原 Agent PID 不变。
2. 通过节点内 DNS/NAT 将真实 `api.github.com` HTTPS 请求导向隔离端点。控制端及下载器没有生产 URL 覆盖开关；测试 CA、路由和私有 token 只存在于可删除节点。
3. 实际 Go API 发现候选版本，私有凭据鉴权成功。明确 POST 升级后，真实 systemd.path 启动下载器。
4. 第一笔下载故意损坏内容：SHA256 拒绝，控制端 PID 不变，没有进入安装事务；修复源后需再次明确提交才执行。
5. 第二次下载、校验和安装成功。控制端 PID 改变，更新服务在独立 cgroup 中继续并写入成功结果。
6. 容器标识、应用意图、项目网络、数据卷/密钥、管理 token、域名及前端 → 后端 → PostgreSQL/Redis 的 HTTPS 只读值保持。
7. 再注入候选服务启动后的后端故障：验证超时，原安装器阻止提交、恢复旧 release 并恢复业务数据链路；随后正常重装通过。

最终采样约 898 MiB / 2 GiB、176 PID，包含测试 CA、测试 API、四个业务容器、运行时及 tmpfs 缓存；这不是生产容量或峰值内存承诺。节点已自动删除。开发日志位于被 Git 忽略的 `target/update-delivery-runtime.log`。

## 验证边界

隔离端点使用真实 TLS、私有鉴权和真实下载字节，但不证明生产 GitHub 的网络可达性或权限配置。远程 CI 构建/Release 发布、真实私有 Release CDN 跳转、腾讯云安全组、公开 DNS/Let's Encrypt、真实 VM 重启或断电、业务备份恢复仍需上线验收。amd64 已编译打包，未在真实业务节点运行完整链路。没有自动发布此次本地代码，也没有自动升级任何生产节点。
