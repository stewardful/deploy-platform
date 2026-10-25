# Deploy Platform

面向个人项目和小型服务的单节点部署平台：**React 管理网页 + Go 控制端 + 可独立运行的 Rust Agent + nerdctl/containerd**。生产安装以 Ubuntu 24.04 / 26.04 LTS、2 核 2 GiB 为目标，当前版本 0.5.3。

支持[版本发现与网页手动升级](docs/updates.md)、预构建镜像部署、应用启停与恢复、项目网络和服务名、数据卷与密钥、日志与 HTTP 健康检查、域名绑定和自动 HTTPS。节点不需要 Node.js 常驻，也不在服务器编译源码。AI API 应用使用相同部署模型；GPU 调度和本地大模型容量不在当前范围。

## 从这里开始

| 你的目标 | 入口 |
| --- | --- |
| 理解目录、组件和请求流程 | [项目地图](docs/project-map.md) |
| 在腾讯云从零安装并部署前后端/数据库/HTTPS | [完整操作手册](docs/tencent-ubuntu-walkthrough.md) |
| 升级平台并保护已有项目 | [升级保障与业务探针](docs/upgrade.md) |
| 使用已有安装包 | [安装、升级与排错](docs/install.md) |
| 开发或修改代码 | [开发指南](CONTRIBUTING.md) |
| 查看当前系统兼容性与验证边界 | [Ubuntu 24.04 / 26.04 验证](docs/ubuntu-compatibility.md) |
| 查看历史完整运行基线 | [0.4.1 测试与修复报告](docs/test-report-0.4.md) |
| 查找其他说明 | [文档目录](docs/README.md) |

## 目录职责

```text
apps/             三个应用：Rust Agent、Go 控制端、React Web
crates/           可复用 Rust 组件，编译进应用，不是独立服务
packages/         可复用 Go / TypeScript 客户端库
contracts/        Agent 与控制端的 OpenAPI 合约
distribution/    安装、发布、手动服务模板及 Linux 验收工具
examples/         普通应用、数据库、AI 与四服务示例
tests/           跨应用浏览器测试、安装组件测试
docs/            设计、使用、验收和维护说明
```

`packages` 是被程序引用的代码包；`distribution` 是把程序构建、打包、安装到服务器的工具。`crates` 与 `packages` 同属可复用代码，只是沿用 Rust 的 crate 命名。详见各目录 README；[9 个 Rust 组件及合并理由](docs/crate-review.md)。

`Cargo.toml` 管理 Rust workspace，`go.work` 管理 Go 模块，`package.json` 管理 npm workspace；它们共同服务一个 monorepo。`target/`、`node_modules/`、Web 构建目录与测试产物不提交到 Git；`sources/` 是本地只读参考，也不提交。

## 安装到 Ubuntu

本 monorepo 同时提供独立 cloudctl 与服务器管理平台。cloudctl 负责下载、校验、安装和升级，安装在 `/opt/cloudctl/releases/`，不从平台 current 目录加载代码。Web/Go/Rust Agent 管理项目；独立的 [deploy-webhook](https://github.com/stewardful/deploy-webhook) 只负责通知触发。

```bash
bash -o pipefail -c 'curl -fsSL --connect-timeout 20 --max-time 120 https://github.com/stewardful/deploy-platform/releases/download/dev-latest/install.sh | sudo bash -s -- --channel dev'
```

一行入口需本改动合并、CI 成功发布后才可下载；当前 PR 阶段不要执行尚未发布的地址。公开 dev 安装无需 GitHub token 或其他安装工具仓库。正式稳定版、固定构建、离线安装和排错见 [统一首次安装](docs/install.md#统一-cloudctl-首次安装)。已有安装保持不变，统一用 `sudo cloudctl install --channel dev` 安装/升级：同构建跳过，新构建复用保护升级；正式更新需 `--yes`。

只安装 cloudctl、暂不安装平台：在上述一行命令末尾加 `--cli-only`。之后服务器只需：

```bash
sudo cloudctl install --channel dev
```

安装完成后用 `sudo cloudctl status` 检查状态，`sudo cloudctl version` 查看版本，`sudo cloudctl token` 仅在自己的终端读取登录凭据。

电脑建立 SSH 隧道后，访问 `http://127.0.0.1:8088/` 登录；管理端保持回环监听。业务域名通过独立 Caddy 提供 HTTPS。准备安装包、示例镜像、密钥、DNS 和端口的完整步骤见 [腾讯云手册](docs/tencent-ubuntu-walkthrough.md)。

应用常用命令已统一到 `cloudctl apps/deploy/start/stop/logs/diagnostics`，通过 `cloudctl help` 查看默认能力；无需 Go/Web 的独立 Agent 用法见 [命令指南](docs/commands.md)。

## 在开发机验证

需要 Rust/C 工具链、Go、Node 和 Python，具体版本与各层检查见 [开发指南](CONTRIBUTING.md)。从仓库根目录执行：

```bash
npm ci
make check
make test
# 首次运行浏览器测试需准备 Chromium
npx playwright install chromium
make web-e2e
```

`make test` 包含真实 SQLite、Unix Socket 和 Go/Rust 集成，容器运行时使用替身。需要 Linux 权限和真实运行时的测试单独执行，见 [验收清单](docs/acceptance.md)。

## 当前边界

- 面向可信管理员、可信镜像和单节点；管理 token 具有节点管理权限，尚无多用户授权或远程 Agent 管理。
- 部署入口是镜像，不自动从 Git 构建，不提供应用配置更新/回滚或私有仓库凭据管理。
- 数据卷保留不等于备份；密钥使用主机私有文件，尚无 KMS/静态加密和自动轮换。
- 后台恢复针对容器进程；HTTP 健康检查为按需快照，不自动触发重启或后台告警。
- 本地 Ubuntu ARM64 已完成真实运行验收；AMD64 与公网云节点的验证边界见测试报告。2 GiB 不代表可以运行任意数量的服务或本地大模型。

Rust 组件暂设 `publish = false`，Go 模块使用本地模块路径。正式对外发布前需确定实际仓库路径、许可证、公共 API 和版本策略；推送 main 会触发 GitHub Release 工作流，dev 推送发布公开预发布包，服务器通过Webhook 通知和本地开关决定 dev 自动升级，配置见 [CI/CD](docs/ci-cd.md)；正式版只检查更新，必须由管理员确认升级。组件库本身尚未发布至 crates.io/npm/Go 公共模块。
