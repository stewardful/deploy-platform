# Web 项目管理工作台

使用 React + TypeScript + TDesign，接入现有 Go 控制 API。生产环境由 Go 同源提供静态文件与 API，浏览器不直接接触 Agent Socket，Ubuntu 不需要 Node.js 或常驻前端进程。

## 当前能力

- 管理令牌登录/退出，校验 Agent 是否就绪。
- 应用列表、项目/应用搜索、本地分页、期望状态、部署阶段和恢复次数。
- 表单部署镜像：项目/应用、镜像、内存/CPU/进程数、网络、端口映射和普通环境/参数。
- 启动/停止，提交后追踪 Agent job；停止保留配置和停止意图。
- 任务中心：本标签页最近 50 个任务，按 ID 查询其他任务，查看结果与失败信息。
- 刷新后重新登录，保留任务 ID 与未确认提交；原请求重试复用同一幂等键。
- 数据卷与项目密钥、数据库模板、诊断与日志、域名绑定与证书检查。
- 桌面与窄屏布局，应用每 10 秒刷新，任务约每 2 秒一轮、最多 4 个并发读取；页面隐藏/离线时暂停轮询。

本版管理预先构建的镜像，已支持应用卷、项目密钥、项目网络、日志/健康诊断及域名自动 HTTPS。AI API Key 使用只读密钥文件。尚无源码构建、应用配置更新/回滚或私有仓库凭据管理；旧 bridge 仍共享，新项目隔离使用 project 网络。

## 目录与复用

```text
package.json / package-lock.json          npm workspace 与固定安装锁
packages/ts/control-client               无框架 TypeScript SDK
apps/web
  src/components                         登录、状态标签、任务详情等通用组件
  src/features/apps                      应用表格、部署表单
  src/features/jobs                       任务列表与查询
  src/features/ingress                    域名与证书管理
  src/hooks                              列表轮询、提交/任务恢复
  src/App.tsx                            工作台组装
apps/controlplane/webui                   Go 静态站点适配器
```

SDK 不依赖 React/TDesign，可在其他前端或 TypeScript 项目中复用。当前 private 包导出 TypeScript 源码，使用方需要 TypeScript/bundler；尚未发布 npm。静态适配器与 API 业务层独立，未设置 --web-dir 时保持纯 API 模式。

组件使用 [TDesign React 官方接入方式](https://github.com/Tencent/tdesign-react)。本仓库锁定实际安装版本，升级时使用锁文件、类型检查和浏览器测试验证。

## 本机开发

在仓库根目录安装依赖（Node >=22.12，部署机无需安装）：

```bash
npm ci
npm run web:dev
```

Vite 默认本机 5173，将 /api 转发到本机 Go 8088。先启动 agentd 和 controld，再用对应令牌登录。开发代理不保存或注入令牌，也不接入远程服务器。开发服务不用于生产部署。

```bash
npm run typecheck
npm test
make web-build
make go-build
```

产物为 apps/web/dist 和 target/controlplane/controld。前端不会在运行时从 CDN 加载组件，不需要互联网访问 UI 资源。

## Ubuntu 安装并打开网页

新节点推荐直接使用 [一键安装](install.md)，它同时安装 Web、控制端、Agent 和独立运行时，目标机不需要 Node。以下步骤用于手动管理的节点，不要覆盖一键安装器的 unit 或混用安装路径。

### 手动安装

先按 [Go 控制端指南](controlplane.md) 配好 Agent、控制端二进制和私有 token；在开发/CI 机器构建网页，上传整个 dist 目录。对 2 核 2 GB 节点，建议把前端构建和镜像构建放在开发机/CI。

```bash
# 开发/CI 机器
npm ci
make web-build
# 根据服务器架构设置 GOARCH=amd64 或 arm64
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath \
  -o target/controlplane/controld-linux-amd64 ./apps/controlplane/cmd/controld
```

将 Linux 二进制、apps/web/dist 全部内容和 unit 上传到服务器后：

```bash
sudo install -d -m 0755 /usr/local/share/deploy-platform/web
sudo cp -R apps/web/dist/. /usr/local/share/deploy-platform/web/
sudo install -m 0755 target/controlplane/controld-linux-amd64 /usr/local/bin/controld
sudo install -m 0644 distribution/manual/systemd/deploy-control.service /etc/systemd/system/deploy-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now deploy-control.service
sudo systemctl restart deploy-control.service
```

升级时先停止控制端，再替换静态产物并重启，避免页面和资源版本短暂不一致；Agent 和业务应用继续运行。首次安装需先创建 token（见控制端指南）；不要覆盖原 token。unit 已带 `--web-dir /usr/local/share/deploy-platform/web`，缺少 index.html 会明确拒绝启动。只上传构建产物，静态目录由管理员控制，不放源码、配置库或令牌。

同机访问 http://127.0.0.1:8088/。在个人电脑访问云服务器时，建立 SSH 隧道：

```bash
ssh -N -L 8088:127.0.0.1:8088 user@server
```

然后在个人电脑浏览器打开 http://127.0.0.1:8088/，输入服务器的管理令牌。Go 保持 loopback 监听，不需要腾讯云开放 8088。令牌代表完整节点管理权限，此阶段不是多用户系统。业务域名/TLS 已在“域名与 HTTPS”提供；管理入口仍走回环/SSH 隧道，尚无多账户登录。

前台使用本机构建结果：

```bash
sudo ./target/controlplane/controld \
  --agent-socket /run/deploy-platform/agent.sock \
  --token-file /etc/deploy-platform/control.token \
  --web-dir "$PWD/apps/web/dist"
```

Go 对 /api 和 /api/* 始终使用原认证/API 路由，未知 API 不会返回 HTML。页面入口不要求令牌，以便显示登录页；业务 API 仍要求 Bearer。前端通过请求头认证，不使用 Cookie。指纹资源长期缓存，index.html 重新验证，不做 SSR 或运行时 JavaScript 构建。

## 提交、恢复与退出

令牌只存在页面内存，不写 localStorage、sessionStorage 或 URL。刷新/退出后需要重新登录；sessionStorage 仅保存最多 50 个 job ID，以及一笔未确认提交的路径、非敏感配置和原 request_id。关闭标签页通常清除此浏览器本地记录；Agent 的任务仍保存。调用方可手动按已知 ID 查询。

请求先在本标签页保存，再发往 Go。202 后显示任务详情，轮询直到成功/失败。状态成功表示节点操作完成，不代表 HTTP 业务健康。进程已启动与期望运行分开显示。

网络错误/超时/队列满等提交未确认时，不自动补新键；先用「使用原请求确认」，原键同请求会返回已接受任务或被重新接受。刷新后也能进行此确认。未确认期间禁止发起新修改；输入错误 400/413/415 明确未接受则释放本地记录，保留表单内容供修改。

「清除本地记录」不取消服务端可能已接受的任务。任务失败也不能通过重提原键重跑，先核对应用当前状态再发起新操作。运行意图可能已由后台自动恢复处理。

## 检查与边界

```bash
npm ci
npm test
npm run web:build
# 首次准备匹配的测试浏览器（只在开发/CI）
npx playwright install chromium
make web-e2e
```

web-e2e 会启动临时真实 Go HTTP 和 Rust Agent HTTP/service/SQLite，只有容器运行时为内存模拟，不操作真实项目。验证登录、部署、应用列表、停止/启动、任务查询、响应丢失后刷新和原键确认，以及窄屏无全页横向溢出。需要允许创建本机测试 Socket 和启动测试浏览器。

不替代 Ubuntu/containerd/CNI、systemd 和服务器重启验收。内存目标仍需在 2 核 2 GB 实机测量；UI 不新增服务器进程，不代表整个节点与业务应用已达到实测资源上限。

0.2.0 新增“数据与密钥”面板、卷与用户编辑器和数据库模板，密钥上传独立于任务草稿，具体边界见 [有状态应用指南](stateful.md)。
