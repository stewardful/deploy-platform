# 文档导航

## 安装与使用

| 文档 | 内容 |
| --- | --- |
| [腾讯云 Ubuntu 完整手册](tencent-ubuntu-walkthrough.md) | 从上传安装包到四服务、域名、HTTPS、重启验收 |
| [安装与升级](install.md) | 文件布局、systemd、升级/失败恢复、安装包构建 |
| [0.5.1 升级保障](upgrade.md) | 兼容性预检、任务排空、迁移预演、原项目与 HTTPS 数据验证 |
| [四服务项目](fullstack.md) | 项目网络、服务名、日志、健康、前后端示例 |
| [数据卷与密钥](stateful.md) | PostgreSQL/Redis、卷保留、密钥边界 |
| [域名与 HTTPS](ingress.md) | 域名、代理、证书、同步和排错 |
| [Web](web.md) | 本地开发与手动网页托管 |
| [Go 控制端](controlplane.md) | API、令牌、Go 构建/手动部署 |
| [独立 Agent](agent.md) | 手动集成、CLI、持久化与恢复语义 |
| [运行恢复](recovery.md) | 停止意图、退避、服务与整机重启的区别 |

## 开发与协议

| 文档 | 内容 |
| --- | --- |
| [模块审查](crate-review.md) | 13→9 个 Rust crate 的保留与合并理由 |
| [命令指南](commands.md) | 统一 cloudctl 与独立 agentctl 的默认行为 |
| [项目地图](project-map.md) | 目录、职责、请求经过的组件、找代码入口 |
| [开发指南](../CONTRIBUTING.md) | 工具链、统一检查、增加模块与 Git 提交范围 |
| [架构](architecture.md) | 依赖、并发、持久化、权限与扩展规则 |
| [本地 API](local-api.md) | Unix HTTP、任务、幂等、错误与资源限制 |
| [兼容性](compatibility.md) | 平台、运行时与功能边界 |
| [任务路线图](tasks.md) | 已完成能力和待办 |
| [Agent OpenAPI](../contracts/agent.openapi.json) / [控制端 OpenAPI](../contracts/control.openapi.json) | 协议合约 |

## 验收与历史证据

当前升级保障的执行结果见 [0.5.1 升级验收](upgrade-validation.md)，操作说明见 [升级保障](upgrade.md)。

先用 [验收清单](acceptance.md)，当前模块/命令回归看 [0.5 审查与验收](crate-review.md)，之前的完整运行基线见 [0.4.1 测试与修复报告](test-report-0.4.md)。以下记录保留当时的版本和执行边界，历史数据不表示当前云节点已经验证：

- [0.1/0.2 安装与有状态验收](install-validation.md)
- [0.3 四服务验收](fullstack-validation.md)
- [0.4 HTTPS 验收](ingress-validation.md)
- [目录重构记录](repository-refactor.md)
- [0.5.0 首次提交前检查](pre-git-audit.md)

- [版本发现与网页升级](updates.md)：GitHub Release、私有仓库凭据、升级状态和恢复。
- [0.5.2 网页升级验收](update-validation.md)：测试结果、隔离系统级流程及未验证边界。

- [dev/main CI/CD](ci-cd.md)：公开发布、本机通知、本地升级策略与排错。

- [Ubuntu 24.04 / 26.04 兼容性](ubuntu-compatibility.md)：0.5.3 的主机预检、安装、升级与验证边界。
