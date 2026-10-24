# 构建与打包

- `build.sh`：准备固定上游归档，在原生 Linux 构建 Rust/Go/Web，调用打包器。
- `package.py`：接收已构建产物，检查 ELF 架构和上游摘要，生成 `.tar.gz`、自解压 `.run` 与 SHA256。`--kind platform-update` 排除外部依赖二进制，`offline` 默认包含全部依赖。
- `runtime.lock.json` / `proxy.lock.json`：已审查的运行时/代理版本与校验值，不单独改下载地址。
- `compatibility.json`：API、状态路径、数据库读写范围；打包时与代理锁一起写入 manifest。发布版本需与 Rust workspace 一致。
- `Dockerfile.agent`：跨开发环境生成对应 Linux Agent 二进制；不用于生产常驻。

仓库根执行 `make release-build`。已有二进制可运行 `python3 distribution/release/package.py --help` 查看输入。输出在 `target/dist/`，不进入 Git。构建和打包都在开发机/CI，不在 2 GiB 生产节点执行。

安装流程在 [installer](../installer/README.md)，操作说明在 [安装指南](../../docs/install.md)。

main 推送触发测试、两种架构构建、自动版本标签及 GitHub Release 完整发布；dev 复用构建流程并通过 SSH 自动部署专用测试机，见 [CI/CD](../../docs/ci-cd.md)；详见 [发布与服务器更新](../../docs/updates.md)。

`PACKAGE_KIND=platform-update make release-build` 用于 dev，跳过上游运行时/Caddy 归档下载与嵌入；平台组件仍完整编译。`RELEASE_OUTPUT` 可指定输出目录。依赖提取、缓存和补齐共用 `installer/lib/dependencies.py`，构建端与服务器不维护两套提取逻辑。详见 [CI/CD](../../docs/ci-cd.md)。
