# 常驻恢复与实机验收

当前后台恢复针对容器进程退出/缺失；HTTP 业务就绪检查已提供按需快照，不作为自动重启条件。默认检查间隔 10 秒，串行处理，失败退避 5 秒起、最多 300 秒，稳定运行 60 秒重置。网络拉取受运行时命令时限约束，不保证全部应用在固定时间内恢复。

## 安装与运行

[独立 Agent 指南](agent.md) 包含构建、安装两个二进制和启用 systemd 的命令。运行时配置只在 agentd 设置，agentctl 连接同一个 API Socket。unit 默认路径需按实际环境调整。

agentd 提供私有 Unix Socket HTTP + JSON API，不监听 TCP；详见 [本地 API](local-api.md)。常规检查使用真实 Socket、SQLite、CLI 子进程及模拟容器；隔离 Ubuntu ARM64 的实际运行时与 systemd 已验收，真实云服务器整机重启仍需按 [上云手册](tencent-ubuntu-walkthrough.md) 验证。

## Ubuntu 验收

1. 检查已安装 nerdctl/containerd/CNI 环境，启用 deploy-agent.service，查看 journal，确认 API Socket 已创建。
2. 使用 agentctl doctor 检查 API/worker，用示例 apply 部署 Web，通过 curl 访问 127.0.0.1:18080；doctor 本身不验证运行时健康。
3. 使用 nerdctl stop 在测试应用上模拟进程退出，等待恢复，确认 HTTP 再次可用。真实删除容器的测试仅针对无业务数据的应用。
4. 重启服务，确认容器仍运行，健康轮询不反复新增部署操作。停止 daemon 不应停止业务容器。
5. agentctl stop --project demo --name web 保存停止意图，重启 daemon，确认容器保持停止、缺失不重建。用 agentctl start 恢复运行意图。
6. 在可接受中断的测试服务器上安排重启，确认 systemd 自动启动 agentd，running 应用可访问，stopped 应用保持停止。自动化测试不会重启服务器。
7. 使用立即退出的测试镜像验证退避，重启 daemon，确认 attempts/next_attempt_ms 保留。异常摘要位于 agentctl apps 的 recovery 字段。
8. 慢拉取期间通过 apply --no-wait 获取任务 ID，确认 apps/job 正常查询；重启服务后继续查询同一任务。检查 Socket 权限和 RuntimeDirectory 的开机重建。

显式启用真实容器测试：

```bash
cargo test -p node-agent --test linux_apply --locked -- --ignored --nocapture
```

使用真实运行时、专用 namespace，覆盖 API 部署、独立 daemon 进程重启后恢复、手动停止后不重建；不等于 systemd 或服务器重启验收。

## 操作语义

- apply：首次保存配置，同配置新任务会表达 running 意图，变更配置仍拒绝。
- stop：先保存 stopped 并清除退避，再尝试停止；动作失败也保留意图。
- start：保存 running 并清除退避，立即恢复；动作失败由后台重试。
- daemon 启动：重放未完成任务后协调应用，不依赖原始 TOML 文件，不重新部署健康应用。
- daemon 退出：取消当前等待，保留意图和任务，不停止业务容器。
- 配置/归属冲突：不强制替换，记录失败并退避。
- 容器丢失：running 应用按保存配置重建；可变 tag 可能变化，推荐 digest。
- 直接 nerdctl stop：不修改管理意图，会被当作故障恢复。

## 锁、资源与故障

daemon 全生命周期独占状态锁，CLI 通过 API 读写。慢拉取期间仍可查询，修改操作排队串行；未完成任务默认上限 32，满时 429，原 request_id 重试。不要用其他状态目录并行管理同一批容器。

持久化退避使用 Unix 毫秒时间，主机应维持正常系统时钟。大幅向后调时导致的超长等待会重新核对。稳定窗口是进程存活的采样观察，不代表没有短暂异常。

HTTP 与 worker 各用当前线程 async runtime，HTTP 的阻塞 SQL 进入最多两个线程的池；没有外部数据库/消息队列服务。稳定轮询不新增历史、不反复写相同状态。资源占用须在 2 核 2 GB 节点实测；systemd MemoryHigh 是软压力设置。

运行时暂不可用时应用退避；存储损坏等节点错误导致 daemon 退出，systemd 限速重启。触发启动限速后需排障，再 reset-failed/start。状态库是私有明文；密钥与业务卷由独立资源组件管理，参见 [数据卷与密钥](stateful.md)。任务/操作历史暂不清理，需关注磁盘。

## API 任务与升级

升级前停止服务并备份 SQLite；v1/v2 自动迁移 v3，旧程序不兼容。agentctl 的运行时/状态目录参数已移除，改为 --socket。日常管理必须启动 agentd；--once 是停服务后的离线维护，有未完成任务时拒绝，避免跳过排队的停止任务。

202 后断线仍继续；SIGTERM 取消等待但保留任务。重启按原 ID 重放，先查真实状态，避免盲目重复创建。提交结果不确定，用原键原命令重试；已知 ID 直接查询。旧失败任务同键仍返回原失败，新执行需新键并先检查应用实际摘要。
