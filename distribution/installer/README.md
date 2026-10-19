# Ubuntu 安装组件

执行顺序：自解压与校验 → 主机/兼容性检查和安装锁 → 维护排空/业务基线 → 状态副本预演 → 快照/激活 → systemd 启动 → 原项目验证/提交事务。应用部署和恢复仍由 Agent 管理。

| 文件/目录 | 职责 |
| --- | --- |
| `bootstrap.sh.in` | `.run` 外壳，验证和解包，再进入安装脚本 |
| `install.sh` | 参数、平台、安装顺序 |
| `lib/os.sh` / `host.sh` | 共用 Ubuntu 版本策略与主机能力检查，CI 在传包前验证 |
| `lib/archive.py` | 安全解压与完整清单/摘要校验 |
| `lib/network.py` / `configure.py` | 网段选择、专属运行时与代理配置 |
| `lib/upgrade_state.py` | 兼容性、SQLite/资源状态、一致性副本预演 |
| `lib/upgrade_probes.py` / `agent_http.py` | 受限只读业务/TLS 探针与私有 Agent HTTP 传输 |
| `lib/upgrade.py` | 排空、基线、停止边界及升级后验证的组装入口 |
| `lib/state.py` / `activate.sh` | 受管理文件快照、事务、激活和失败恢复 |
| `systemd/` | 一键安装器专用的四服务模板 |
| `cloudctl` | 已安装节点的管理/诊断命令 |
| `*_smoke.py` / `smoke.py` | 节点健康、普通应用和数据库/四服务验收 |

不可变发布目录不能写入 Python 缓存。秘密、业务数据和证书存储位于独立私有目录；升级保留不等于业务备份。手动模板位于 [manual](../manual/README.md)，不能覆盖本安装器管理的 unit。

[业务探针与升级保障](../../docs/upgrade.md) · [安装路径与升级边界](../../docs/install.md) · [源码安装组件测试](../../tests/README.md)

- `lib/update_worker.py`：由独立 `deploy-update.path/service` 按需调用，下载受信 GitHub Release 并调用原安装器；控制端只有更新目录的写权限，升级过程不受其重启影响。
