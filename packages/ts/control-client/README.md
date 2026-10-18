# Control v1 TypeScript SDK

无 React、TDesign 或存储依赖的浏览器同源 SDK。根 npm workspace 自动解析本地包；private 包导出 TypeScript 源码，需要 TypeScript/bundler，尚未发布 npm。

```ts
import { ControlClient } from '@deploy-platform/control-client';
const client = new ControlClient(token);
const apps = await client.apps(signal);
const job = await client.job('123', signal);
```

提供应用/任务、密钥与卷、诊断与日志、域名和证书查询，参数类型对应 [控制端 OpenAPI](../../../contracts/control.openapi.json)。服务器仍是权威校验方，validateSpec 提供表单即时反馈。默认只访问 /api/v1 相对路径，不支持用户输入远程 URL。

令牌由调用方以内存管理，SDK 不保存令牌、不使用 Cookie、不自动重试。submit 必须提供稳定 request_id；请求结果不确定时，调用方保存原路径/请求体并重试原请求，不能换键重投。失败终态通过 Job.state/error 表达。

读取支持 AbortSignal、8 秒总时限、1 MiB 响应上限；POST 正文最大 256 KiB。JS 无法安全表示的任务 ID 会拒绝，避免精度损失后查询错误任务。可注入 fetcher 用于测试。Node 服务端请使用 Go SDK/自定义本地传输，本包默认面向浏览器同源场景。
