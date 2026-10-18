# 跨模块检查

| 位置 | 内容 | 入口 |
| --- | --- | --- |
| `installer/` | 安装/发布的归档、配置、事务与缓存回归 | `make installer-test` |
| `installer/test_upgrade.py` | 升级兼容性、迁移预演、门禁超时、业务/TLS 与恢复故障注入 | `make installer-test` |
| `web/` | 浏览器 → Go → Rust fixture，运行时为替身 | `make web-e2e` |
| `check_docs.py` | 本地 Markdown 链接，跳过产物与只读参考 | `make docs-check` |

组件测试跟随源码：Rust 位于各 crate/app，Go 为包内 `*_test.go`，TS SDK 测试位于 SDK 源码中。需要真实 Linux/runtime 权限的测试环境在 [distribution/tests](../distribution/tests/)，不要与普通单元测试的执行条件混淆。

完整基线看 [开发指南](../CONTRIBUTING.md)，云端人工验收看 [腾讯云手册](../docs/tencent-ubuntu-walkthrough.md)。

平台更新测试覆盖 Go 版本发现/候选校验/私有凭据/并发门禁、Python 流式下载与摘要/跳转/中断，以及浏览器提示和确认。系统级验证可用：

```bash
CLOUD_TEST_WEB_UPDATE=1 bash distribution/tests/upgrade-smoke.sh /ABS/bootstrap-with-updater.run /ABS/newer.run /ABS/demo.tar
```

此模式只在脚本创建的可删除 Ubuntu 节点内安装隔离 GitHub HTTPS 测试端点与 CA，通过真实 Go API、私有凭据、systemd.path 和下载器执行升级；不会配置生产自定义下载地址。启动包必须含网页更新器且版本低于目标包；演示镜像需为 `cloud-fullstack-demo:upgrade-check`。默认模式保留原有手动升级验收。

CI 交付组件测试见 `tests/installer/test_ci.py`：版本/架构策略、main 提交标签、发布幂等、root 执行器与 SSH 参数校验。
