import { describe, it, expect, vi } from 'vitest';
import {
  ControlClient,
  ControlError,
  isSubmission,
  validateSpec,
  validJobID,
  type AppSpec,
  type Submission,
} from './index';
const spec: AppSpec = {
  schema_version: 1,
  app: { project: 'demo', name: 'web' },
  image: 'example/web:v1',
  resources: { memory_mib: 128, cpu_millis: 500, pids: 64 },
};
const submission: Submission = {
  path: '/api/v1/apps/apply',
  body: { request_id: 'stable-key', spec },
  summary: '部署 demo/web',
};
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
describe('portable control client', () => {
  it('calls browser fetch with its global receiver', async () => {
    const original = globalThis.fetch;
    globalThis.fetch = function (this: unknown) {
      expect(this).toBe(globalThis);
      return Promise.resolve(json({ api_version: 'v1', service_version: 'test', accepting: true }));
    };
    try {
      expect((await new ControlClient('token').health()).accepting).toBe(true);
    } finally {
      globalThis.fetch = original;
    }
  });
  it('validates application model boundaries', () => {
    expect(validateSpec(spec)).toBeNull();
    expect(validateSpec({ ...spec, app: { project: 'Demo', name: 'web' } })).toContain('项目');
    expect(validateSpec({ ...spec, ports: [{ host: 18080, container: 8080 }] })).toContain(
      'bridge',
    );
    expect(validateSpec({ ...spec, args: ['bad\0value'] })).toContain('参数');
    expect(validateSpec({ ...spec, environment: { BAD: '😀'.repeat(3000) } })).toContain(
      '环境变量',
    );
    expect(validJobID('9223372036854775807')).toBe(false);
    expect(validJobID('1')).toBe(true);
  });
  it('does not allow persisted requests to target an arbitrary host or endpoint', () => {
    expect(isSubmission(submission)).toBe(true);
    expect(isSubmission({ ...submission, path: 'https://example.invalid' })).toBe(false);
    expect(isSubmission({ ...submission, path: '/api/v1/apps/../health' })).toBe(false);
  });
  it('retains key, sends no cookies and never retries an uncertain POST', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('network failure'));
    const client = new ControlClient('private-token', fetcher);
    await expect(client.submit(submission)).rejects.toMatchObject({
      status: 0,
      detail: { request_id: 'stable-key' },
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [path, options] = fetcher.mock.calls[0];
    expect(path).toBe('/api/v1/apps/apply');
    expect(JSON.parse(String(options?.body)).request_id).toBe('stable-key');
    expect(options).toMatchObject({ credentials: 'omit', redirect: 'error', cache: 'no-store' });
  });
  it('preserves queue and auth errors instead of treating them as accepted jobs', async () => {
    const client = new ControlClient(
      'token',
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(json({ code: 'queue_full', message: 'full', retryable: true }, 429)),
    );
    await expect(client.submit(submission)).rejects.toMatchObject({
      status: 429,
      detail: { code: 'queue_full' },
    });
    const auth = new ControlClient(
      'token',
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(
          json({ code: 'unauthorized', message: 'bad token', retryable: false }, 401),
        ),
    );
    await expect(auth.apps()).rejects.toMatchObject({ status: 401 });
  });
  it('rejects mismatched IDs, malformed responses and response floods', async () => {
    for (const response of [
      json({ id: 3, request_id: 'wrong-key', state: 'queued', result: null, error: null }, 202),
      new Response('x', { status: 202 }),
      new Response('x'.repeat(1024 * 1024 + 1), {
        status: 202,
        headers: { 'content-type': 'application/json' },
      }),
    ]) {
      const client = new ControlClient('token', vi.fn<typeof fetch>().mockResolvedValue(response));
      await expect(client.submit(submission)).rejects.toBeInstanceOf(ControlError);
    }
  });
  it('returns failed terminal jobs as data and cancels reads through AbortSignal', async () => {
    const job = {
      id: 7,
      request_id: 'key',
      state: 'failed',
      result: null,
      error: { code: 'unavailable', message: 'failure', retryable: true },
    };
    const client = new ControlClient('token', vi.fn<typeof fetch>().mockResolvedValue(json(job)));
    expect((await client.job(7)).state).toBe('failed');
    const abort = new AbortController();
    abort.abort();
    const cancelled = new ControlClient(
      'token',
      vi.fn<typeof fetch>().mockRejectedValue(new DOMException('Aborted', 'AbortError')),
    );
    await expect(cancelled.apps(abort.signal)).rejects.toMatchObject({ name: 'AbortError' });
  });
});

it('validates private mount boundaries and immutable stateful references', () => {
  const base = {
    schema_version: 1 as const,
    app: { project: 'demo', name: 'db' },
    image: 'postgres:17-bookworm',
    resources: { memory_mib: 384, cpu_millis: 500, pids: 128 },
    user: { uid: 999, gid: 999 },
    volumes: [{ name: 'data', target: '/var/lib/postgresql/data' }],
    secrets: [{ name: 'db-password' }],
  };
  expect(validateSpec(base)).toBeNull();
  expect(validateSpec({ ...base, volumes: [{ name: 'data', target: '/etc' }] })).not.toBeNull();
  expect(
    validateSpec({ ...base, volumes: [{ name: 'data', target: '/data/../escape' }] }),
  ).not.toBeNull();
  expect(
    validateSpec({
      ...base,
      volumes: [
        { name: 'data', target: '/data' },
        { name: 'other', target: '/data/nested' },
      ],
    }),
  ).not.toBeNull();
});

it('discovers and explicitly approves upgrades without passing URLs or commands', async () => {
  const status = {
    enabled: true,
    current: '0.5.1',
    candidate: null,
    checked_at: '',
    error: '',
    execution: null,
  };
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(json(status, 200))
    .mockResolvedValueOnce(json(status, 202))
    .mockResolvedValueOnce(json(status, 202));
  const client = new ControlClient('token', fetcher);
  await client.updates();
  await client.checkUpdates();
  await client.upgrade('0.5.2');
  expect(fetcher.mock.calls.map(([path]) => path)).toEqual([
    '/api/v1/updates',
    '/api/v1/updates/check',
    '/api/v1/updates/upgrade',
  ]);
  expect(fetcher.mock.calls[2][1]?.body).toBe('{"version":"0.5.2"}');
});
