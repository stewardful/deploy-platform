/** Framework-free control API SDK. Same-origin only; no token storage or automatic retries. */
export interface AppKey {
  project: string;
  name: string;
}
export interface AppSpec {
  pull_policy?: 'always' | 'if_not_present' | 'never';
  schema_version: 1;
  app: AppKey;
  image: string;
  resources: { memory_mib: number; cpu_millis: number; pids: number };
  network?: 'none' | 'bridge' | 'project';
  health_check?: { port: number; path: string };
  ports?: { host: number; container: number }[];
  environment?: Record<string, string>;
  args?: string[];
  user?: { uid: number; gid: number };
  volumes?: { name: string; target: string; read_only?: boolean }[];
  secrets?: { name: string }[];
}
export interface SecretInfo {
  project: string;
  name: string;
}
export interface SecretInput extends SecretInfo {
  value: string;
}
export interface VolumeInfo {
  name: string;
  target: string;
  uid: number;
  gid: number;
}
export function validVolumeTarget(s: string): boolean {
  return (
    s.length <= 256 &&
    !s.endsWith('/') &&
    /^(?:\/data|\/var\/lib|\/srv|\/opt|\/app|\/workspace)(?:\/|$)/.test(s) &&
    s
      .slice(1)
      .split('/')
      .every((p) => /^[A-Za-z0-9._-]+$/.test(p) && p !== '.' && p !== '..')
  );
}
export interface Application {
  app: AppKey;
  fingerprint: string;
  phase: string;
  instance_id: string | null;
  desired: 'running' | 'stopped';
  recovery: {
    attempts: number;
    next_attempt_ms: number;
    running_since_ms: number;
    error_code: string | null;
  };
}
export interface Health {
  api_version: string;
  service_version: string;
  accepting: boolean;
}
export interface APIError {
  code: string;
  message: string;
  retryable: boolean;
  request_id?: string;
}
export interface Job {
  id: number;
  request_id: string;
  state: 'queued' | 'running' | 'succeeded' | 'failed';
  result: Record<string, unknown> | null;
  error: APIError | null;
}
export interface ApplyRequest {
  request_id: string;
  spec: AppSpec;
  startup_wait_ms?: number;
}
export interface Submission {
  path: string;
  body: ApplyRequest | { request_id: string };
  summary: string;
}
export const terminal = (job: Job) => job.state === 'succeeded' || job.state === 'failed';
export const validJobID = (value: string) =>
  /^[1-9]\d*$/.test(value) && Number.isSafeInteger(Number(value));
const identifier = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;
const validName = (value: string) => value.length <= 48 && identifier.test(value);
const uint = (value: number, min: number, max = 4294967295) =>
  Number.isInteger(value) && value >= min && value <= max;
const validValue = (s: string) =>
  typeof s === 'string' && new TextEncoder().encode(s).length <= 8192 && !s.includes('\0');
export function validateSpec(spec: AppSpec): string | null {
  if (spec.pull_policy && !['always', 'if_not_present', 'never'].includes(spec.pull_policy))
    return '镜像拉取策略无效';
  const h = spec.health_check;
  if (
    h &&
    (spec.network !== 'project' ||
      !Number.isInteger(h.port) ||
      h.port < 1 ||
      h.port > 65535 ||
      h.path.length > 256 ||
      !/^\/[A-Za-z0-9/._~-]*$/.test(h.path) ||
      h.path.startsWith('//'))
  )
    return 'HTTP 健康检查需要项目网络、有效容器端口和路径';
  if (spec.schema_version !== 1) return '配置版本必须为 1';
  if (!validName(spec.app.project) || !validName(spec.app.name))
    return '项目和应用名称请使用 1–48 位小写字母、数字或横线，首尾不能是横线';
  if (!/^[A-Za-z0-9][A-Za-z0-9/._:@-]*$/.test(spec.image) || spec.image.length > 512)
    return '请输入有效的镜像仓库引用';
  if (
    spec.image.includes('@') &&
    !/^sha256:[a-fA-F0-9]{64}$/.test(spec.image.split('@').slice(1).join('@'))
  )
    return '镜像 digest 格式不正确';
  const r = spec.resources;
  if (!uint(r.memory_mib, 16) || !uint(r.cpu_millis, 1) || !uint(r.pids, 1))
    return '内存至少 16 MiB，CPU 配额和进程数必须为正整数';
  if (spec.network && !['none', 'bridge', 'project'].includes(spec.network)) return '网络模式无效';
  if (spec.user && (!uint(spec.user.uid, 0) || !uint(spec.user.gid, 0)))
    return 'UID/GID 必须为非负整数';
  const volumes = spec.volumes ?? [],
    secrets = spec.secrets ?? [];
  if (volumes.length > 8 || secrets.length > 16) return '最多 8 个数据卷、16 个密钥引用';
  if (
    new Set(volumes.map((v) => v.name)).size !== volumes.length ||
    volumes.some(
      (v, i) =>
        !validName(v.name) ||
        !validVolumeTarget(v.target) ||
        volumes
          .slice(0, i)
          .some(
            (p) =>
              p.target === v.target ||
              p.target.startsWith(v.target + '/') ||
              v.target.startsWith(p.target + '/'),
          ),
    )
  )
    return '数据卷名称、路径无效或挂载路径重叠';
  if (
    new Set(secrets.map((s) => s.name)).size !== secrets.length ||
    secrets.some((s) => !validName(s.name))
  )
    return '密钥引用名称无效或重复';
  const ports = spec.ports ?? [];
  if (
    ports.length > 16 ||
    (ports.length > 0 && !['bridge', 'project'].includes(spec.network ?? ''))
  )
    return '端口映射需要 bridge 网络，最多 16 个';
  if (
    new Set(ports.map((p) => p.host)).size !== ports.length ||
    ports.some((p) => !uint(p.host, 1024, 65535) || !uint(p.container, 1, 65535))
  )
    return '端口不可重复，主机端口为 1024–65535，容器端口为 1–65535';
  const env = spec.environment ?? {};
  if (
    Object.keys(env).length > 128 ||
    Object.entries(env).some(
      ([k, v]) => k.length > 128 || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(k) || !validValue(v),
    )
  )
    return '环境变量格式无效；最多 128 个，每个值不超过 8192 字节';
  if ((spec.args ?? []).length > 128 || (spec.args ?? []).some((a) => !validValue(a)))
    return '启动参数格式无效';
  return null;
}
export function requestID(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
    b.toString(16).padStart(2, '0'),
  ).join('');
}
export class ControlError extends Error {
  constructor(
    public status: number,
    public detail: APIError,
  ) {
    super(detail.message);
    this.name = 'ControlError';
  }
  get uncertain() {
    return this.status === 0 || this.status === 408 || this.status >= 500;
  }
}
export function isSubmission(value: unknown): value is Submission {
  if (!value || typeof value !== 'object') return false;
  const s = value as Submission;
  return (
    typeof s.path === 'string' &&
    /^\/api\/v1\/apps\/(?:apply|[a-z0-9-]+\/[a-z0-9-]+\/(?:start|stop))$/.test(s.path) &&
    typeof s.summary === 'string' &&
    s.summary.length <= 200 &&
    !!s.body &&
    typeof s.body === 'object' &&
    typeof s.body.request_id === 'string' &&
    /^[A-Za-z0-9_-]{1,128}$/.test(s.body.request_id) &&
    new TextEncoder().encode(JSON.stringify(s)).length <= 256 * 1024
  );
}
function parseJob(value: Job): Job {
  if (
    !value ||
    !validJobID(String(value.id)) ||
    typeof value.request_id !== 'string' ||
    !['queued', 'running', 'succeeded', 'failed'].includes(value.state)
  )
    throw protocol();
  return value;
}
const protocol = () =>
  new ControlError(502, {
    code: 'agent_protocol',
    message: '服务返回的数据格式不正确',
    retryable: false,
  });
export interface Diagnostics {
  state: string;
  network: string | null;
  service_name: string | null;
  health: 'healthy' | 'unhealthy' | 'not_running' | 'not_configured';
  http_status: number | null;
  checked_at_ms: number;
}
export interface LogSnapshot {
  text: string;
  tail: number;
}
export interface PlatformUpdate {
  notifications: {
    connected: boolean;
    dev_auto_upgrade: boolean;
    dev_tag: string;
    error: string;
    last_attempt: {
      schema_version: number;
      repository: string;
      run_id: string;
      phase: string;
    } | null;
  };
  enabled: boolean;
  current: string;
  candidate: { version: string; asset_id: number; size: number; digest: string } | null;
  checked_at: string;
  error: string;
  execution: { version: string; phase: string; message: string } | null;
}

export class ControlClient {
  constructor(
    private token: string,
    private fetcher: typeof fetch = (input, init) => globalThis.fetch(input, init),
  ) {}
  async updates(): Promise<PlatformUpdate> {
    return this.request('/api/v1/updates');
  }
  async checkUpdates(): Promise<PlatformUpdate> {
    return this.request('/api/v1/updates/check', {});
  }
  async upgrade(version: string): Promise<PlatformUpdate> {
    return this.request('/api/v1/updates/upgrade', { version });
  }
  async routes(): Promise<IngressSnapshot> {
    return this.get<IngressSnapshot>('/api/v1/ingress');
  }
  async changeRoute(route: DomainRoute, expected_revision: number): Promise<RouteReceipt> {
    if (
      !validDomain(route.domain) ||
      !validName(route.app.project) ||
      !validName(route.app.name) ||
      !Number.isInteger(route.port) ||
      route.port < 1 ||
      route.port > 65535 ||
      !['production', 'staging'].includes(route.tls) ||
      !Number.isSafeInteger(expected_revision) ||
      expected_revision < 0
    )
      throw new Error('域名、应用、容器端口或版本无效');
    return this.request<RouteReceipt>('/api/v1/ingress', { route, expected_revision });
  }
  async deleteRoute(domain: string, expected_revision: number): Promise<RouteReceipt> {
    if (!validDomain(domain) || !Number.isSafeInteger(expected_revision) || expected_revision < 1)
      throw new Error('域名或版本无效');
    return this.request<RouteReceipt>('/api/v1/ingress/delete', { domain, expected_revision });
  }
  async certificate(domain: string): Promise<CertificateStatus> {
    if (!validDomain(domain)) throw new Error('域名无效');
    return this.get<CertificateStatus>(`/api/v1/ingress/certificate/${domain}`);
  }
  async createSecret(input: SecretInput): Promise<SecretInfo> {
    if (
      !validName(input.project) ||
      !validName(input.name) ||
      !input.value ||
      new TextEncoder().encode(input.value).length > 65536 ||
      input.value.includes('\0')
    )
      throw new Error('密钥名称或内容无效，最多 64 KiB');
    const result = await this.request<SecretInfo>('/api/v1/secrets', input);
    if (result.project !== input.project || result.name !== input.name || 'value' in result)
      throw protocol();
    return result;
  }
  async secrets(project: string): Promise<SecretInfo[]> {
    if (!validName(project)) throw new Error('项目名称无效');
    return this.get<SecretInfo[]>(`/api/v1/secrets/${project}`);
  }
  async diagnostics(app: AppKey): Promise<Diagnostics> {
    if (!validName(app.project) || !validName(app.name)) throw new Error('应用标识无效');
    return this.get<Diagnostics>(`/api/v1/diagnostics/${app.project}/${app.name}`);
  }
  async logs(app: AppKey, tail = 100): Promise<LogSnapshot> {
    if (!validName(app.project) || !validName(app.name)) throw new Error('应用标识无效');
    if (!Number.isInteger(tail) || tail < 1 || tail > 500) throw new Error('日志行数应为 1..500');
    return this.get<LogSnapshot>(`/api/v1/logs/${app.project}/${app.name}?tail=${tail}`);
  }
  async volumes(app: AppKey): Promise<VolumeInfo[]> {
    if (!validName(app.project) || !validName(app.name)) throw new Error('项目或应用名称无效');
    return this.get<VolumeInfo[]>(`/api/v1/volumes/${app.project}/${app.name}`);
  }
  async health(signal?: AbortSignal): Promise<Health> {
    const data = await this.get<Health>('/api/v1/health', signal);
    if (!data || data.api_version !== 'v1' || !data.accepting) throw protocol();
    return data;
  }
  async apps(signal?: AbortSignal): Promise<Application[]> {
    const data = await this.get<Application[]>('/api/v1/apps', signal);
    if (
      !Array.isArray(data) ||
      data.some(
        (a) =>
          !a?.app ||
          typeof a.app.name !== 'string' ||
          typeof a.app.project !== 'string' ||
          !a.recovery,
      )
    )
      throw protocol();
    return data;
  }
  async job(id: string | number, signal?: AbortSignal): Promise<Job> {
    if (!validJobID(String(id))) throw new Error('请输入有效的任务 ID');
    const data = parseJob(await this.get<Job>(`/api/v1/jobs/${id}`, signal));
    if (String(data.id) !== String(id)) throw protocol();
    return data;
  }
  async submit(submission: Submission, signal?: AbortSignal): Promise<Job> {
    if (!isSubmission(submission)) throw new Error('提交请求无效');
    const data = parseJob(await this.request<Job>(submission.path, submission.body, signal));
    if (data.request_id !== submission.body.request_id) throw protocol();
    return data;
  }
  private get<T>(path: string, signal?: AbortSignal) {
    return this.request<T>(path, undefined, signal);
  }
  private async request<T>(
    path: string,
    body?:
      | Submission['body']
      | SecretInput
      | { route: DomainRoute; expected_revision: number }
      | { domain: string; expected_revision: number }
      | { version: string }
      | Record<string, never>,
    signal?: AbortSignal,
  ): Promise<T> {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    else signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, 8000);
    try {
      const encoded = body ? JSON.stringify(body) : undefined;
      if (encoded && new TextEncoder().encode(encoded).length > 256 * 1024)
        throw new Error('请求超过 256 KiB，请减少配置内容');
      const response = await this.fetcher(path, {
        method: body ? 'POST' : 'GET',
        headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
        body: encoded,
        signal: controller.signal,
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
      });
      if (!response.headers.get('content-type')?.toLowerCase().startsWith('application/json'))
        throw protocol();
      const reader = response.body?.getReader();
      if (!reader) throw protocol();
      const chunks: Uint8Array[] = [];
      let size = 0;
      try {
        while (true) {
          const part = await reader.read();
          if (part.done) break;
          size += part.value.byteLength;
          if (size > 1024 * 1024) throw protocol();
          chunks.push(part.value);
        }
      } finally {
        await reader.cancel().catch(() => {});
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
      }
      let data: unknown;
      try {
        data = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
      } catch {
        throw protocol();
      }
      if (!response.ok) {
        const detail = data as APIError;
        if (!detail || typeof detail.code !== 'string' || typeof detail.message !== 'string')
          throw protocol();
        throw new ControlError(response.status, detail);
      }
      if (response.status !== (body ? 202 : 200)) throw protocol();
      return data as T;
    } catch (error) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      if (
        error instanceof ControlError ||
        (error instanceof Error && error.message.startsWith('请求超过'))
      )
        throw error;
      throw new ControlError(0, {
        code: 'transport',
        message: body
          ? '连接中断或超时，部署可能已被接受。请保留原请求重试确认。'
          : '无法连接控制端，请稍后刷新。',
        retryable: true,
        request_id: body && 'request_id' in body ? body.request_id : undefined,
      });
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }
}

export interface DomainRoute {
  domain: string;
  app: AppKey;
  port: number;
  tls: 'production' | 'staging';
  enabled: boolean;
}
export interface RouteStatus {
  route: DomainRoute;
  revision: number;
  proxy_state: string;
  upstream_ready: boolean;
  checked_at_ms: number;
  error_code: string | null;
}
export interface IngressSnapshot {
  routes: RouteStatus[];
  revision: number;
  applied_revision: number;
  checked_at_ms: number;
  error_code: string | null;
}
export interface RouteReceipt {
  revision: number;
}
export interface CertificateStatus {
  domain: string;
  state: string;
  trusted: boolean;
  not_after: string | null;
  issuer: string | null;
  checked_at_ms: number;
}
export function validDomain(value: string): boolean {
  const labels = value.split('.');
  const last = labels.at(-1) ?? '';
  return (
    value.length <= 253 &&
    labels.length >= 2 &&
    labels.every((s) => s.length <= 63 && identifier.test(s)) &&
    last.length >= 2 &&
    (/^[a-z]+$/.test(last) || last.startsWith('xn--')) &&
    !['localhost', 'local', 'internal', 'invalid', 'example', 'test'].includes(last)
  );
}
