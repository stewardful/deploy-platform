# Ubuntu 24.04 / 26.04 安装兼容性

0.5.3 支持 Ubuntu 24.04 和 26.04 LTS，amd64 / arm64，systemd 与 cgroup v2；不放开其他发行版或未验证的 Ubuntu 版本。服务器只运行预编译程序。

## 实现边界

- `distribution/installer/lib/os.sh` 是系统版本策略，安装器、缺少 Python 时的引导以及本机主机检查共用。打包时将相同策略嵌入自解压外壳。
- 本地安装前检查系统、架构、systemd/cgroup、sudo 和可用磁盘；不符合要求时拒绝安装。
- 发布父目录与 releases 目录显式设置为 0755，避免 `umask 077` 影响动态代理用户遍历目录。密钥、管理配置和数据库仍在原私有目录，不放宽其权限。
- 这是目标系统的应用兼容性，不包含在服务器上执行 Ubuntu 发行版升级。

## 可复用验收

测试只操作自身创建的可删除 Docker 节点，需要支持特权容器和 cgroup 的 Linux Docker 服务。每个节点限制 2 CPU / 2 GiB；默认 Ubuntu 24.04，通过 `UBUNTU_VERSION=26.04` 选择新系统。

```bash
# 26.04 安装、重装、PostgreSQL/Redis 数据与恢复
UBUNTU_VERSION=26.04 STATEFUL=1 bash distribution/tests/ubuntu-smoke.sh /ABS/deploy-platform-0.5.3-linux-arm64.run

# 26.04 维护升级路径与回退；历史 0.5.2 不支持在 26.04 首次安装，故先安装 0.5.3 后验收重装
UBUNTU_VERSION=26.04 bash distribution/tests/upgrade-smoke.sh /ABS/deploy-platform-0.5.3-linux-arm64.run /ABS/deploy-platform-0.5.3-linux-arm64.run /ABS/upgrade-demo.tar

# 24.04 上真实历史版本到新版的升级
UBUNTU_VERSION=24.04 bash distribution/tests/upgrade-smoke.sh /ABS/deploy-platform-0.5.2-linux-arm64.run /ABS/deploy-platform-0.5.3-linux-arm64.run /ABS/upgrade-demo.tar

# 一行首次安装：CLI 同目录有 install.sh 与校验；需要当前构建的公开入口契约
UBUNTU_VERSION=26.04 bash distribution/tests/bootstrap-smoke.sh /ABS/dev/deploy-platform-0.5.3-linux-arm64.run /ABS/cloudctl

```

`upgrade-demo.tar` 必须包含与节点架构一致、标记为 `cloud-fullstack-demo:upgrade-check` 的验收镜像。HTTPS 使用隔离测试 CA，覆盖前端、后端、PostgreSQL、Redis、业务数据保留、缺少探针时提前拒绝、注入后端故障后回退及正常重装。测试不会连接腾讯云服务器。

本地原生 ARM64 验证 Ubuntu 26.04 完整链路。amd64 在本地架构模拟环境可启动平台服务，但嵌套业务容器启动失败，因此不将模拟结果计为完整安装验收；PR 另在原生 amd64 GitHub runner 上分别测试 24.04 / 26.04 的真实容器安装流程。容器共享宿主 Linux 内核，不能据此证明腾讯云实际内核、AppArmor、重启断电或公网 Let's Encrypt 的行为。正式服务器安装仍需后续验收。

[安装指南](install.md) · [升级保障](upgrade.md) · [CI 与交付](ci-cd.md)
