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


一行入口：`tests/installer/test_install_entry.py` 验证固定 CLI 地址与摘要、默认关闭自动升级、错误/已有安装/超时保护。`bash distribution/tests/bootstrap-smoke.sh /ABS/update.run /ABS/cloudctl` 在全新 Ubuntu 26.04 节点执行实际 HTTPS curl 管道，覆盖损坏 CLI 拒绝、缺少 Python 自动准备、正式入队安装、服务验收及重复执行不改动安装/凭据。cloudctl 同目录须有 CI 生成的 install.sh 和两个校验文件；本地构造 fixture 使用 dev-42，CI 使用当前 run ID；可选第三/四参数提供锁定 runtime/Caddy 归档以避免重复下载。HTTPS 源为本机测试 CA 和固定 GitHub 路径的隔离 fixture，不是生产下载覆盖参数，也不连接用户服务器。
