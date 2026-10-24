# 安装与分发

本目录负责把程序做成安装包并部署到节点，不是应用可引用的 SDK，也不承载容器业务逻辑。原目录名 `packaging/` 已统一改为 `distribution/`，没有并行的旧入口。

| 目录 | 使用场景 | 入口 |
| --- | --- | --- |
| `cli/` | 构建统一管理命令 | `python3 -B distribution/cli/package.py` |
| `release/` | 开发机/CI 构建与打包 | `build.sh`、`package.py`、运行时/代理锁定清单 |
| `ci/` | 版本、正式发版、工作流检查、SSH 指令 | `version.py`、`dev-release.py`、`release.py`、`deploy-test.sh` |
| `installer/` | `.run` 在 Ubuntu 上的安装、升级与自检 | `bootstrap.sh.in`、`install.sh`、`cloudctl` |
| `installer/systemd/` | 一键安装器的专属运行时与服务 | containerd、Agent、Go/Web、Caddy 四个 unit |
| [manual](manual/README.md) | 已有运行时的手动集成 | `manual/systemd/`，仅 Agent 和控制端 |
| `tests/` | 可删除 Linux 节点验收和测试构建 | `ubuntu-smoke.sh`、`Dockerfile.rust`、`https-runtime.py` |

常见操作从仓库根执行：

```bash
make installer-test
# 仅在准备好的原生 Linux 构建机执行，不在 2 GiB 目标节点编译
make release-build
```

生成 `target/dist/*.run`、`.tar.gz`、`.sha256`，不提交 Git。dev 只接收对应架构的平台更新包，按版本复用基础依赖；正式离线包仍包含全部运行时。仅接受当前安装包和状态格式，不保留历史格式推断或兼容入口。

`release/package.py` 以仓库根为基准收集源文件，安装包内部保持原 `lib/`、`systemd/` 等布局；升级记录和受管理服务名不变。

[安装指南](../docs/install.md) · [手工 Agent 集成](../docs/agent.md) · [测试与上云验收](../docs/acceptance.md)

`cli/` 构建通用 cloudctl 命令；首次安装和后续升级共用 installer/lib 中的下载、状态和安装逻辑。

公开 dev 预发布分发 cloudctl 和两架构平台更新包，无需服务器 token；自动部署通过 `TEST_SERVER_AUTO_DEPLOY=true` 单独开启，默认关闭。
