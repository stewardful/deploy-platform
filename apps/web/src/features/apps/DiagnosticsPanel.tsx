import { useEffect, useState } from 'react';
import { Alert, InputNumber, Tag } from 'tdesign-react';
import {
  ControlError,
  type AppKey,
  type ControlClient,
  type Diagnostics,
} from '@deploy-platform/control-client';
import { Modal } from '../../components/Modal';
import { Button } from '../../components/Button';

const healthText: Record<string, string> = {
  healthy: 'HTTP 健康',
  unhealthy: 'HTTP 不健康',
  not_running: '容器未运行',
  not_configured: '未配置 HTTP 检查',
};
export default function DiagnosticsPanel({
  client,
  app,
  onClose,
  onUnauthorized,
}: {
  client: ControlClient;
  app: AppKey;
  onClose: () => void;
  onUnauthorized: () => void;
}) {
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [logs, setLogs] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const [tail, setTail] = useState(100);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      setBusy(true);
      const results = await Promise.allSettled([client.diagnostics(app), client.logs(app, tail)]);
      if (cancelled) return;
      const messages: string[] = [];
      for (const result of results)
        if (result.status === 'rejected') {
          if (result.reason instanceof ControlError && result.reason.status === 401) {
            onUnauthorized();
            return;
          }
          messages.push(result.reason instanceof Error ? result.reason.message : '查询失败');
        }
      if (results[0].status === 'fulfilled') setDiagnostics(results[0].value);
      else setDiagnostics(null);
      if (results[1].status === 'fulfilled') setLogs(results[1].value.text);
      else setLogs('');
      setErrors(messages);
      setBusy(false);
      timer = setTimeout(() => void refresh(), 10000);
    };
    void refresh();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [client, app, tail, revision, onUnauthorized]);
  return (
    <Modal
      visible
      label="应用诊断"
      header={`${app.project} / ${app.name}`}
      width={880}
      onClose={onClose}
      footer={false}
    >
      <div className="diagnostics-toolbar">
        <Button
          loading={busy}
          disabled={busy}
          variant="outline"
          onClick={() => setRevision((n) => n + 1)}
        >
          刷新诊断
        </Button>
        <label>
          日志行数
          <InputNumber
            min={1}
            max={500}
            value={tail}
            onChange={(v) => {
              const n = Number(v);
              if (Number.isInteger(n) && n >= 1 && n <= 500) setTail(n);
            }}
          />
        </label>
      </div>
      {errors.map((message, i) => (
        <Alert key={i} theme="warning" message={message} />
      ))}
      {diagnostics && (
        <div className="diagnostics-summary">
          <Tag
            theme={
              diagnostics.health === 'healthy'
                ? 'success'
                : diagnostics.health === 'unhealthy'
                  ? 'danger'
                  : 'default'
            }
          >
            {healthText[diagnostics.health] ?? diagnostics.health}
          </Tag>
          <p>
            容器：{diagnostics.state} · HTTP：{diagnostics.http_status ?? '—'}
          </p>
          <p>
            项目网络：<code>{diagnostics.network ?? '无网络'}</code> · 服务名：
            <code>{diagnostics.service_name ?? '—'}</code>
          </p>
          <small>
            检查于 {new Date(diagnostics.checked_at_ms).toLocaleTimeString()}，打开此面板时每 10
            秒刷新。健康检查不触发自动重启。
          </small>
        </div>
      )}
      <h4>应用日志</h4>
      <p className="muted">
        最多 500 行 / 64 KiB。stdout 与 stderr 分组显示。应用可能自行打印敏感信息，请谨慎复制。
      </p>
      <pre className="application-logs" tabIndex={0} aria-label="应用日志">
        {logs || (busy ? '正在读取…' : '暂无日志')}
      </pre>
    </Modal>
  );
}
