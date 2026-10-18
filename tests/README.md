# 跨模块检查

| 位置 | 内容 | 入口 |
| --- | --- | --- |
| `installer/` | 安装/发布的归档、配置、事务与缓存回归 | `make installer-test` |
| `web/` | 浏览器 → Go → Rust fixture，运行时为替身 | `make web-e2e` |
| `check_docs.py` | 本地 Markdown 链接，跳过产物与只读参考 | `make docs-check` |

组件测试跟随源码：Rust 位于各 crate/app，Go 为包内 `*_test.go`，TS SDK 测试位于 SDK 源码中。需要真实 Linux/runtime 权限的测试环境在 [distribution/tests](../distribution/tests/)，不要与普通单元测试的执行条件混淆。

完整基线看 [开发指南](../CONTRIBUTING.md)，云端人工验收看 [腾讯云手册](../docs/tencent-ubuntu-walkthrough.md)。
