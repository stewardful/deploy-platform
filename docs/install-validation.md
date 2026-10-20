# 一键安装验收记录

本文保留历史版本的验证记录；当前命令以 [命令指南](commands.md) 为准。

此记录针对本地构建的 0.1.0 安装组件，不代表已经部署到腾讯云。最终安装包通过纯打包组件生成，目标服务器不会编译源码。

## 已实际验证

- Ubuntu 24.04、Linux ARM64、systemd PID 1、cgroup v2；在隔离 Docker 后端内限制 2 CPU、2 GiB、512 PID，无主机目录挂载。
- 使用最终 `.run` 从全新环境安装 dedicated containerd / nerdctl、Rust Agent、Go 控制端、Web；三个 systemd 服务健康，HTTP 登录页可访问，未认证 API 返回 401。
- 重复安装成功；token 摘要一致，保留配置和持久化状态。
- 使用 Go HTTP API 真实拉取并部署非 root Nginx 镜像；CNI bridge + 回环端口 HTTP 200；检查 CPU 0.25、内存 128 MiB、64 PID；相同 request_id 返回原任务。
- 停止测试容器并重启 Agent 后，已保存的运行意图自动恢复 HTTP 服务；最后通过 API 持久化停止意图并删除本次测试容器。
- 构造无法启动的控制端进行失败升级：健康检查超时后恢复旧 release 链接、配置与原活动服务；原 token 不变，健康检查重新通过，SQLite 备份保留，事务记录清除。
- 模拟中断事务：保存快照与事务记录，写入部分配置、停止控制服务；重跑安装器先恢复原状态，再成功安装。
- 安装组件 8 项回归测试；覆盖归档路径/链接/特殊文件拒绝、摘要与完整文件清单、ELF 架构、RFC1918 /24 路由冲突、私有运行时配置、文件备份恢复与事务验证。ShellCheck 与语法检查通过。
- Rust workspace、Go 单元/Socket 集成回归、前端 7 项 SDK 测试和生产构建通过；实际安装测试补齐了 nerdctl 空状态与非 root 文件权限的兼容修复。

完整脚本 `distribution/tests/ubuntu-smoke.sh` 成功输出：

```text
PASS: real image pull, CNI loopback HTTP, resource limits, idempotency and Agent restart recovery
PASS: fresh install, repeat install, token preservation and real project recovery
```

测试应用清理后的容器观测值约 **124.3 MiB / 2 GiB、CPU 0.04%**。这是一次包含 systemd、平台进程、镜像缓存等的测试容器观测，使用 tmpfs 数据盘；不是腾讯云 VM 的整机内存测量，也不是容量承诺。应用数、镜像解压、文件缓存与 AI 模型会显著改变使用量。

## 验证边界

未连接腾讯云实例，未测试腾讯云安全组/公网反向代理、实际服务器断电与整机重启，也未进行长时间压力测试。测试环境使用专属可执行 tmpfs 避免嵌套 overlayfs 限制；生产使用正常磁盘。第一次上云后按照安装指南完成真实镜像拉取、HTTP 检查、服务器重启恢复和停止意图验收。

amd64 与 arm64 均已构建离线安装包，全部文件清单及 sidecar SHA256 校验通过；使用各自的 Linux 二进制和固定架构运行时包。完整 systemd / 容器生命周期验收在 ARM64 后端完成；不能把它视为 AMD64 云主机的运行验收。GitHub 发布工作流已提供两种原生 runner 配置，但本地没有触发远程 CI 或对外发布。

## 0.2.0 有状态应用追加验收

在限制 2 CPU / 2 GiB 的 Ubuntu 24.04 ARM64 systemd/cgroup v2 隔离节点，安装 0.2.0 并实际执行 `cloudctl stateful-smoke`。真实拉取 PostgreSQL 17-bookworm 与 Redis 7.4-bookworm，均以 UID/GID 999:999、禁用全部 capability、no-new-privileges 运行。

- PostgreSQL 使用只读密码文件认证，写入表记录；移除容器、重启 Agent 自动创建新容器，读回原记录。
- Redis 使用只读 requirepass 配置文件认证，AOF 持久化写入；移除容器、重启 Agent 后读回原键。
- 普通 API、任务结果与容器 inspect 不包含上传的密码值；节点内部审计确认 SQLite/WAL 和 Agent/控制端日志中没有该值，审计不输出密码。
- 通过 API 保存两个应用的停止意图并移除测试容器，再次重启 Agent：期望状态仍为 stopped，没有新容器；数据卷和项目密钥保留。
- Rust 工作区、Clippy、Go/Rust Socket 联调和 race、前端校验测试通过；真实浏览器验证上传只返回名称、清空输入、浏览器存储不包含值，以及 PostgreSQL 模板部署通过。

完整自检输出 `PASS: postgres authenticated write + container recreation + persisted read + secret references`、相应 Redis PASS 及 `stateful applications stopped; durable data and project secrets retained`。验收结束后测试节点约 772.7 MiB / 2 GiB，其中 tmpfs 保留了数据库镜像和数据，不能当作生产 VM 的空闲内存测量。真实腾讯云节点、整机断电和长时负载仍未验收；明文文件权限保护不等于静态加密，数据卷保留不等于业务备份。0.2.0 API/安装限制见 [有状态指南](stateful.md)。

最终 0.2.0 安装包追加执行了带资源目录的升级验收：升级前后在节点内部比较所有卷文件、密钥文件与 token 的内容摘要，全部一致，平台健康检查通过。amd64/arm64 安装包的完整文件清单、自解压载荷与 sidecar SHA256 均通过校验；完整数据库生命周期验收仍仅在 ARM64 Linux 后端执行。
