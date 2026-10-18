# 开发与维护

从仓库根运行以下命令。先读 [项目地图](docs/project-map.md)，确认修改属于应用、库还是安装分发。

## 工具链

- Rust workspace 最低 1.85；发布构建使用 1.96。需要 C 编译器构建 bundled SQLite，开发检查需 rustfmt/clippy。
- Go 控制端/SDK 最低 1.23；四服务示例是独立 Go 1.26 模块，用 1.26.9 工具链验证。
- Node ≥22.12（发布构建使用 Node 24）、npm；提交根 npm 锁文件。
- Python 3（安装器要求的 Ubuntu 24.04 自带 3.12；开发建议 3.11+）、Bash；完整 Shell 静态检查需要 ShellCheck。
- Docker 只用于可选 Linux 测试/构建，目标 Ubuntu 安装不依赖 Docker。

## 常用命令

```bash
npm ci
make help
make check
make test
npx playwright install chromium
make web-e2e
```

`check` 包含 Rust lint/feature、Go 格式/vet、TS/格式、安装脚本语法和文档链接；`test` 包含 Rust、真实 Go/Rust Socket 集成、TS SDK 和安装器测试。Socket/浏览器测试要求本机允许创建 Unix Socket、回环监听和启动 Chromium；不能把权限拦截当作组件测试通过。

ShellCheck 单独执行 `make shellcheck`；Linux CI 也会检查它。只检查文档执行 `make docs-check`；只看某层可使用 `make rust-test`、`make go-integration`、`make web-test`、`make installer-test`。浏览器回归不运行真实 containerd。

Web 开发执行 `npm run web:dev`，代理与 token 设置见 [Web 指南](docs/web.md)。独立 Agent 开发可以用 `agentctl validate/plan`（TOML/JSON）；测试 fixture 只用于开发，不作为生产 daemon。

## 真实 Linux 与安装包

参考 [验收清单](docs/acceptance.md)、[distribution](distribution/README.md)。需要管理员权限的容器/网络测试只在显式准备的可删除节点执行。不要在 2 GiB 生产服务器编译源码或执行隔离 ACME 续期测试。

`make release-build` 仅用于准备好工具链的原生 Linux 构建机。`distribution/release/package.py` 可接受预编译的二进制、Web 和固定版本运行时归档。改源码目录不应改变安装包内部路径或服务器状态路径。

## 修改约定

1. 应用入口负责组装，模型和库不读取全局服务器配置；通用实现不要在 Rust、Go、Web 各复制一份。
2. 协议变化同步 OpenAPI、Rust、Go/TS SDK、调用方和对应集成测试。SDK 字段当前手工维护。
3. 增加 Rust crate 后更新 workspace、对应目录 README 和项目地图。增加 npm/Go 模块后同步其 workspace 配置。
4. 安装器/发布路径变化需要更新 Makefile、CI、Docker 构建、测试与文档，并实际生成和核验安装包。
5. 成功状态必须说明实际验证范围。普通测试、运行时替身、Linux 容器、真实云服务器、公网证书分别记录。

## 首次提交 Git 前

应提交源码、文档、工作流、语言与上游版本锁文件。`target/`、`node_modules/`、`apps/web/dist/`、测试输出、私有状态、环境密钥和 `sources/` 本地参考均已忽略；安装包由构建/发布流程交付，不放进源码仓库。

本机专用的 `AGENTS.md`、编辑器配置及私有凭据文件同样忽略；忽略规则不能代替提交前检查暂存内容。历史验收文档保留版本和验证范围，当前版本入口见 [文档导航](docs/README.md)。

仍需由项目所有者确定仓库地址、许可证和正式模块发布路径；不要把本地 `deploy-platform.local` 模块误当成已发布 SDK。配置远程仓库和推送须由项目所有者明确安排。
