# 手动服务模板

仅供已有 containerd/nerdctl 环境的手动部署或嵌入集成使用：

- `systemd/deploy-agent.service` 默认使用 `/usr/local/bin/agentd`、系统运行时 `/run/containerd/containerd.sock`。
- `systemd/deploy-control.service` 默认使用 `/usr/local/bin/controld`，静态 Web 放在 `/usr/local/share/deploy-platform/web`。

这些模板不会安装专属运行时、CNI、Caddy 或证书存储，路径由管理员协调。不要覆盖一键安装器管理的同名 unit，也不要把两套模板的路径混用。

新服务器优先使用 [一键安装](../../docs/install.md)。手动配置见 [Agent 指南](../../docs/agent.md)、[Go 指南](../../docs/controlplane.md)。一键安装的模板只有 [installer/systemd](../installer/systemd/) 这一套入口。
