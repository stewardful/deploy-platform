# 0.5.1 升级保障验收记录

执行日期：2026-10-19（北京时间）。开发机 macOS ARM64；真实容器验收使用可删除 Ubuntu 24.04 ARM64/systemd/cgroup v2 节点，限制 2 CPU / 2 GiB / 512 PID。未连接用户腾讯云主机。

## 代码与组件回归

| 检查 | 结果 |
| --- | --- |
| Rust workspace | 76 项通过，1 项独立 Linux 运行时专项默认忽略 |
| 维护门禁 | 慢部署排空、拒绝新任务/密钥/域名写入、读取保持可用、恢复接单通过 |
| 启动与迁移 | start_drained 阻止启动恢复；新 CLI 仅迁移隔离副本；后续 SQL 步骤失败时整体事务回滚通过 |
| Rust 组件裁剪与质量 | fmt、Clippy `-D warnings`、协议-only/client/server、ingress 无/有 Caddy 通过 |
| Linux 与最低工具链 | Linux Rust 测试通过；Rust 1.85 workspace/all-targets 检查通过 |
| Go | race、vet、真实 Go→Rust Socket；门禁跨资源/域名装饰层，Go HTTP 写入返回 503、读取仍可用 |
| TypeScript / Web | SDK 8 项、类型/格式、生产构建通过；已有 Chromium 浏览器 3 项通过 |
| 安装器 | 35 项通过，包含原有 10 项及新增升级兼容性、磁盘、schema、排空、资源、探针与恢复故障注入 |
| Shell / 文档 | Bash 语法、ShellCheck、本地文档链接与 diff 空白检查通过 |
| 外部依赖 | Cargo registry 包的版本、来源和摘要与首次提交一致；新增 SQLite 测试依赖复用已有版本 |

## 真实 2 CPU / 2 GiB 节点

可复用入口为 [upgrade-smoke.sh](../distribution/tests/upgrade-smoke.sh) 与 [upgrade-runtime.py](../distribution/tests/upgrade-runtime.py)。实际执行包括：

1. 从 0.5.0 安装包初始化独立 containerd/nerdctl，部署前端、后端、PostgreSQL 17、Redis 7.4，保留数据库与缓存记录。
2. 在测试节点启动隔离 Caddy CA，通过真实 ACME HTTP-01 签发前端域名证书；只在该节点信任 CA，验证 HTTPS 前端 → 后端 → PostgreSQL/Redis 的只读数据接口。
3. 缺少业务探针配置时，0.5.1 预检拒绝升级；旧 Agent PID、当前 release 和业务访问保持正常。
4. 配置私有探针后，0.5.0→0.5.1 升级通过。安装器核对容器 ID、配置/意图、域名、资源及稳定数据值；管理令牌不变，事务与维护标记被清除。
5. 新版服务启动后，测试专用钩子停止后端。维护状态阻止自动恢复掩盖故障；平台健康检查可通过，但原项目验证失败，安装事务不能提交。
6. 失败恢复返回先前 release/配置，解除维护标记、启动原活动服务；后端恢复，同一 HTTPS 接口仍读到原 PostgreSQL 与 Redis 值。
7. 再次安装同一 0.5.1 包，经过原生 Rust maintenance API 排空、私有数据库预演及完整业务验证后成功。

测试日志出现完整的升级、故障拒绝、恢复和再次安装 PASS；失败注入仅针对测试项目，节点在结束后删除。一次验收结束时容器观测约 0.9–1.0 GiB / 2 GiB，包含 tmpfs 镜像/数据缓存，不代表生产 VM 空载内存或服务容量承诺。

## 产物与边界

生成 `target/dist/deploy-platform-0.5.1-linux-{amd64,arm64}.{run,tar.gz,sha256}`；两架构 Rust/Go ELF、上游摘要、完整文件清单和自解压校验通过。安装包包含兼容性契约、新升级组件及操作文档；真实升级运行在 ARM64，AMD64 未做业务运行验收。

本地日志保留在被 Git 忽略的 `target/upgrade-*`。没有验证腾讯云真实主机、公网 CA、整机重启、断电/磁盘硬件故障或业务备份恢复。自动升级只支持已声明兼容、可回到旧程序读取范围的状态布局；运行时/代理变化、降级与破坏性迁移会拒绝。

应用检查依赖管理员配置的无副作用接口与稳定数据标记；它们不能证明业务数据库所有记录逐字节一致，不能代替独立业务备份。操作与限制见 [升级保障](upgrade.md)。
