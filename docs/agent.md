# 独立 Rust Agent：手动部署与运行语义

新服务器优先使用 [一键安装](install.md)。本文面向已有运行时或嵌入集成的开发者，不能与一键安装器管理的 unit 混用。代码入口见 [Agent 目录](../apps/agent/README.md)。

## Ubuntu 手动部署普通 Web 应用

前提：安装了兼容的 nerdctl、containerd、runc、CNI bridge/portmap 插件，containerd 正在运行；使用 cgroup v2。新节点优先使用上方一键安装；以下为接入已有运行时的手动方式，不要与安装器管理的路径混用。默认 nerdctl 路径 `/usr/local/bin/nerdctl`，Socket `/run/containerd/containerd.sock`，namespace `deploy-platform`，状态目录 `/var/lib/deploy-platform`。运行时配置只由 agentd 参数设置。API Socket 默认 `/run/deploy-platform/agent.sock`；agentctl 仅需同一个 `--socket`，不接触数据库或 containerd。

在开发/CI 环境执行 `cargo build --release --locked -p node-agent --bins`，将对应 Linux 架构产物和示例上传。以下命令在目标节点执行，路径按已上传的产物调整；不要在 2 GiB 节点编译：

```bash
# 在独立终端前台运行，或按下文安装 systemd 服务
sudo ./target/release/agentd
# 以下在另一个终端执行
sudo ./target/release/agentctl doctor
sudo ./target/release/agentctl apply -f examples/http-service.toml
curl http://127.0.0.1:18080
sudo ./target/release/agentctl apps
sudo ./target/release/agentctl operations --limit 10
```

示例使用非特权 Nginx 镜像，限制 128 MiB / 0.5 CPU / 64 进程，显式启用共享 bridge，将容器 8080 映射到 `127.0.0.1:18080`，不会直接发布到公网。默认无配置时仍是 network=none。旧版 bridge 仍共享；新应用可使用 project 网络及应用服务名，见 [完整部署指南](fullstack.md)。应用公网域名和自动 HTTPS 已提供，见 [域名指南](ingress.md)；手动模板本身不安装 Caddy。

重复运行相同 apply：已有且指纹匹配的运行容器会被复用；处于 created/exited 的匹配容器会被启动；没有容器才拉取并创建。inventory 错误不会被当成资源不存在。

## 持久化与失败语义

- API 先事务保存任务并返回 202，单 worker 再保存规范化配置和 Pending 操作，然后执行运行时调用。apply/start/stop 默认等待任务结束，`--no-wait` 仅提交，`agentctl job <ID>` 查询结果。
- SQLite 保存配置、指纹、实例 ID、阶段、操作记录及时间；apps/operations 展示持久化摘要，不泄漏配置值，也不是实时状态查询。
- 状态目录新建时 0700，数据库/锁文件新建时 0600；拒绝权限过宽的状态目录和符号链接数据库。数据库没有加密，只能保存非敏感设置。
- agentd 全生命周期独占状态库租约；CLI 全部通过 API 读写，部署期间仍可查询，无状态锁竞争。agentd.lock 排除第二个 daemon；不要用其他状态目录管理同一批容器。
- 状态绑定 containerd Socket 路径和 namespace，换目标必须使用其他状态目录。
- 进程中断后，下一次打开存储会标记未完成操作为 Interrupted；重新 apply 会先检查实际容器，不盲目创建。
- 使用新请求键的相同配置重试会新增操作记录，但不重复创建已有容器；原键重提返回原任务，不新增操作。配置变更或现有容器指纹不符会明确拒绝，不执行自动替换或删除。
- 创建/启动超时可能已经改变实际状态；失败保留容器供重试或排障，不自动强制清理。
- 成功仅表示短暂观察后容器仍处于 Running，不代表 HTTP 就绪或业务健康；默认观察 1000 ms，可设置 `--startup-wait-ms 100..30000`。
- agentd 运行时，持久化 running 应用退出后会重启，容器缺失时会重建；启用 systemd 服务后服务器重启也会恢复。停止 agentd 不会停止应用；未启动 agentd 时管理 API 不可用。后台自动恢复只检查进程状态；HTTP 就绪检查可按需查询，不作为自动重启条件。
- 指纹标识配置，不是已解析的镜像 digest。同名可变 tag 不会自动触发更新；容器丢失后重建可能拉到变化后的 tag。需要可重复版本时请填写 `repo@sha256:...`。
- 数据卷和密钥文件已接入，应用日志命令已提供；配置更新与回退尚未接入。不要将 API Key 放进 environment 或 args。

状态文件位置：`agent.sqlite`、`agent.sqlite-wal`、`agent.sqlite-shm`、`agent.lock`、`agentd.lock`。备份要通过 SQLite 备份机制或先停止使用状态库，不能在写入时只复制主数据库。

## 常驻恢复与开机启动

在目标 Linux 架构的开发/CI 环境构建两个程序（低内存构建时可加 `-j 1`）：

```bash
cargo build --release --locked -p node-agent --bins
sudo install -m 0755 target/release/agentctl /usr/local/bin/agentctl
sudo install -m 0755 target/release/agentd /usr/local/bin/agentd
sudo install -m 0644 distribution/manual/systemd/deploy-agent.service /etc/systemd/system/deploy-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now deploy-agent.service
sudo systemctl status deploy-agent.service
sudo journalctl -u deploy-agent.service -n 50
```

模板默认使用相同 nerdctl 路径、Socket、namespace 和状态目录；自定义路径时修改 unit 或使用 systemd override。unit 以 root 运行以访问节点运行时，不提供公网管理接口。这些手动模板不由一键安装器使用；一键安装的隔离 Ubuntu 验证与专属模板见安装指南。

手动前台运行或仅做一次协调：

```bash
sudo agentd --poll-seconds 10
sudo agentd --once
```

`--once` 是停止常驻服务后的离线维护模式，不提供 API；有未完成 API 任务时拒绝运行，需正常启动 agentd 先处理。失败、退避中或 Busy 的检查返回非零。日常管理始终通过 API。

默认恢复策略：启动先重放未完成任务，然后检查应用；单 worker 串行处理部署/启停和恢复，默认每轮恢复结束后间隔 10 秒。HTTP 在独立的当前线程异步运行时处理，数据库读写仅短时加锁，阻塞查询进入有界线程池。失败或启动后再次崩溃按 5/10/20/.../300 秒退避，次数和下次时间持久化。稳定运行 60 秒后重置退避。轮询间隔也会影响实际恢复延迟；单个慢拉取/启动可能延迟后续应用。健康轮询不新增部署历史，不反复写相同观察状态。

手动停止/启动必须通过管理 CLI 保存意图：

```bash
sudo agentctl stop --project demo --name web
sudo agentctl start --project demo --name web
sudo agentctl apps
```

stop 先保存 stopped，再尝试停止；即使运行时暂不可用，daemon 后续也只会尝试停止，不会拉起。stopped 应用缺失时不重建。start 保存 running 并清空退避；相同配置 apply 也会重新表达 running 意图。直接用 nerdctl stop 或杀死应用进程会被视为故障并恢复。

SIGTERM / Ctrl+C 会取消当前恢复工作并释放锁，不主动停止业务容器。未完成部署在重新打开状态库时标记 Interrupted，然后核对实际状态。unit 使用 KillMode=process，让 daemon 自己取消受控 CLI，并保留可能需要继续运行的 runtime helper；因此这是可信节点管理进程配置，不适用于运行任意用户子进程。MemoryHigh=192M 是软压力设置，包含 daemon 的辅助进程，不是已测得的内存保证，可按实测调整。

新数据库直接创建当前 v3 结构；已有数据库仅接受 v3，v1/v2 或未知结构会被拒绝，不执行历史迁移。升级前停止 daemon 并做可恢复的状态库备份。恢复次数、下次重试时间和停止意图都在数据库内，不依赖配置源文件仍然存在。

更详细的实机验收见 [常驻恢复操作说明](recovery.md)。

## 质量检查与真实 Linux 测试

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo doc --workspace --no-deps --locked
```

常规测试使用真实 SQLite、真实 Unix Socket HTTP 服务、真实 CLI 子进程、模拟运行时/命令执行器。覆盖并发查询、队列上限、幂等键冲突、等待超时不取消、任务跨重启重放及旧容量队列优先于恢复。它们验证组件流程，不证明实际 Linux 容器可用。

真实测试默认忽略，需在已准备的 Linux 节点显式启用：

```bash
cargo test -p node-agent --test linux_apply --locked -- --ignored --nocapture
```

可通过 `AGENT_TEST_NERDCTL` / `AGENT_TEST_SOCKET` 指定测试运行时。测试创建独立 namespace 和唯一应用，验证 HTTP 200、两次 CLI apply 复用容器、常驻 agentd 重启后拉起退出容器、手动停止后不再重建，并尽力清理自身资源。它需要相应运行时权限和镜像仓库网络访问，不要在未准备的生产节点执行。真实 Linux 专项已在隔离 Ubuntu ARM64 执行，具体范围见 [测试报告](test-report-0.4.md)。
