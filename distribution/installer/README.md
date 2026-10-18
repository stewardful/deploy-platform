# Ubuntu 安装组件

执行顺序：自解压与校验 → 主机检查/安装锁 → 生成配置 → 快照/激活 → systemd 启动 → 健康检查/提交事务。应用部署和恢复仍由 Agent 管理。

| 文件/目录 | 职责 |
| --- | --- |
| `bootstrap.sh.in` | `.run` 外壳，验证和解包，再进入安装脚本 |
| `install.sh` | 参数、平台、安装顺序 |
| `lib/archive.py` | 安全解压与完整清单/摘要校验 |
| `lib/network.py` / `configure.py` | 网段选择、专属运行时与代理配置 |
| `lib/state.py` / `activate.sh` | 受管理文件快照、事务、激活和失败恢复 |
| `systemd/` | 一键安装器专用的四服务模板 |
| `cloudctl` | 已安装节点的管理/诊断命令 |
| `*_smoke.py` / `smoke.py` | 节点健康、普通应用和数据库/四服务验收 |

不可变发布目录不能写入 Python 缓存。秘密、业务数据和证书存储位于独立私有目录；升级保留不等于业务备份。手动模板位于 [manual](../manual/README.md)，不能覆盖本安装器管理的 unit。

[安装路径与升级边界](../../docs/install.md) · [源码安装组件测试](../../tests/README.md)
