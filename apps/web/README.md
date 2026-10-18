# Web 管理界面

React + TypeScript + TDesign，构建为静态文件，由 Go 控制端托管。

- `src/App.tsx`：页面组装和导航。
- `src/features/apps/`：部署、应用列表、数据/密钥、诊断和日志。
- `src/features/jobs/`：任务查询。
- `src/features/ingress/`：域名与 HTTPS 管理。
- 通信使用 [control-client](../../packages/ts/control-client/README.md)，避免组件各自实现网络协议。

在仓库根执行 `npm ci`、`npm run web:dev` 或 `make web-build`。本地开发代理与登录要求见 [Web 指南](../../docs/web.md)。`dist/` 为构建产物，不提交。

前端测试分两层：`npm test` 检查 SDK；`make web-e2e` 检查真实 Go/Rust API 与浏览器交互，运行时使用替身。不要把浏览器测试当作真实容器/公网 HTTPS 验收。
