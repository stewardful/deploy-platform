# 0.4.1 目录整理历史记录

这是 0.4.1 阶段的记录，以下路径和数量描述当时状态。0.5.0 又合并了部分 Rust crate；当前结构见 [项目地图](project-map.md) 和 [模块审查](crate-review.md)。首次本地提交前的最终检查见 [提交前检查记录](pre-git-audit.md)。

本次整理保留版本 0.4.1 的应用和协议行为，调整源码组织、维护入口和文档。未初始化 Git、提交或推送；未修改 `sources/` 只读参考。

## 路径迁移

| 原位置 | 当前位置 | 原因 |
| --- | --- | --- |
| 安装/发布工具目录 | `distribution/` | 与 `packages/` 可复用代码包区分 |
| 手动 systemd 模板 | `distribution/manual/systemd/` | 与安装器专属的 `installer/systemd/` 明确区分 |
| 根 README 的长篇 Agent 手动操作 | `docs/agent.md` | 根文档聚焦项目介绍、目录与使用入口 |

Makefile、CI、Docker 测试入口、打包器、安装器测试和文档同步迁移，不保留平行旧路径。安装包内部布局、服务名、服务器 `/opt`/`/etc`/`/var/lib` 路径和持久化 schema 不因源码目录重命名变化。已有安装包仍可使用，重新构建时使用新源码路径。

## 导航与开发入口

- [项目地图](project-map.md)：从目录职责、一次部署和一次域名绑定理解组件。
- 应用、Rust crates、Go/TS packages、分发工具各有 README；应用下也有启动与维护入口说明。
- [文档索引](README.md) 区分安装、开发、当前能力和历史验收。
- [开发指南](../CONTRIBUTING.md) 与 `make help/check/test/docs-check` 提供统一入口。
- 文档链接检查纳入 CI；只检查本地文档目标，不访问外网。
- `.gitignore` 排除本地产物、Python 缓存、私有环境文件与同步参考；锁文件保留提交。

更新了恢复、API、Web、Go 控制端与路线图中的过时能力描述。保留历史测试记录的版本和验证边界，不把过去的本地验收写成云节点已经通过。

## 验证

2026-10-18，本轮通过：

| 检查 | 结果 |
| --- | --- |
| `make check` | Rust lint/feature、Go 格式/vet、TS/格式、安装脚本语法通过 |
| `make test` | Rust 69、TS 8、安装组件 9，以及 Go/Rust Socket 集成通过；Rust 默认忽略 1 项真实运行时测试 |
| `make web-e2e` | 构建入口和 3 项浏览器回归通过 |
| `make docs-check` | 39 份 Markdown 的本地链接目标通过；另用临时目录验证缺失链接能被发现、产物/参考会跳过 |
| 新路径下两架构打包 | `.run --check`、SHA256 通过；每架构 71 个核心二进制/运行时/安装文件与已有 0.4.1 摘要相同 |
| `distribution/tests/ubuntu-smoke.sh` | 全新 Ubuntu ARM64，限制 2 CPU/2 GiB：安装、重复安装、真实 PostgreSQL/Redis 持久化、自检后再次安装、token 保留通过；自动删除节点 |
| ShellCheck | 新路径下全部安装/发布 shell 通过 |
| Git 忽略规则 | 独立临时 Git 仓库验证产物/参考/私有状态被忽略，源码/锁文件可提交；没有初始化当前项目 |

检查日志和重新生成的验证包在本地 `target/refactor-*.log`、`target/refactor-dist/`，均不进入 Git。临时 ShellCheck 节点也在验收后删除。

本轮是目录和文档重构回归，没有重新执行隔离 CA 自动续期、默认忽略的 Linux 专项、真实腾讯云/public ACME 或服务器整机重启；之前的执行证据及边界继续见 [0.4.1 测试报告](test-report-0.4.md)。

## 首次提交范围

提交源码、文档、Makefile、语言/运行时锁文件与 `.github/` 工作流；不提交 `target/` 中已有安装包/镜像/日志、依赖目录、Web dist、测试输出、私有 `.env`/数据库或 `sources/`。

项目仍需所有者确定正式仓库地址和许可证，然后再决定 SDK 公共发布路径。目录重构不自动发布 npm/crates.io，也不声称许可证已经确定。
