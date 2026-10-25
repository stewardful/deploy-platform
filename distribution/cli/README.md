# cloudctl 与一行安装入口

| 文件 | 职责 |
| --- | --- |
| `package.py` / `entry.py` | 构建架构无关 cloudctl，调用现有 installer/lib；始终运行自己的受校验代码，不转到平台 current |
| `install.sh.in` | root、系统与架构检查，准备依赖，下载校验 CLI，先注册独立 CLI，再调用幂等 install / status / version；支持 --cli-only |
| `installer_entry.py` | 将共享 OS 策略、固定 Release 标签与 CLI SHA256 渲染进 install.sh；发布前验证精确来源 |

对外的 install.sh 是薄引导；`distribution/installer/install.sh` 是安装包内部的受保护安装流程。两者不重复实现下载平台包、systemd worker、任务锁、数据迁移、激活与回退。服务器不编译 Rust/Go/Web，通知触发由独立 deploy-webhook 负责。

本地构建示例（dev-42 仅供隔离测试，不会发布）：

```bash
python3 -B distribution/cli/package.py --output target/cli
python3 -B distribution/cli/installer_entry.py --directory target/cli --repository stewardful/deploy-platform --tag dev-42
bash target/cli/install.sh --help
```

发布 CLI artifact 包含 cloudctl、install.sh 及各自 SHA256。固定 dev 构建和正式 Release 分发全部资产，dev-latest 提供当前入口。脚本引用固定构建的 CLI，摘要随脚本嵌入，不在安装时拼接不同版本的滚动资产。初始脚本依赖官方 HTTPS 来源，不宣称同源 SHA256 是独立签名。

默认入口与高级手动方式见 [安装指南](../../docs/install.md#统一-cloudctl-首次安装)。组件测试和全新 Ubuntu 安装测试见 [测试目录](../../tests/README.md)。

独立路径 `/opt/cloudctl/releases/<sha>/cloudctl`，平台激活和回退不重定向它。平台包携带同一 ZIP，离线安装仅在工具缺失时初始化；后续平台更新保留已安装工具。安装与升级统一为 install；不存在 CLI 别名兼容。
