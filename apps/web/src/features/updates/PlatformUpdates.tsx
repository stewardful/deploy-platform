import { useCallback, useEffect, useState } from 'react';
import { Alert } from 'tdesign-react';
import {
  ControlError,
  type ControlClient,
  type PlatformUpdate,
} from '@deploy-platform/control-client';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';

const phases: Record<string, string> = {
  queued: '排队中',
  checking: '确认版本',
  downloading: '下载安装包',
  verifying: '验证安装包',
  installing: '升级与检查',
  succeeded: '已完成',
  failed: '未完成',
  interrupted: '执行中断',
  skipped: '未执行升级',
};
const terminal = new Set(['succeeded', 'failed', 'interrupted', 'skipped']);
export function PlatformUpdates({
  client,
  onUnauthorized,
}: {
  client: ControlClient;
  onUnauthorized: () => void;
}) {
  const [status, setStatus] = useState<PlatformUpdate | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState('');
  const active = !!status?.execution && !terminal.has(status.execution.phase);
  const failure = useCallback(
    (error: unknown) => {
      if (error instanceof ControlError && error.status === 401) onUnauthorized();
      else setError('暂时无法连接更新服务。升级期间页面可能断开，将自动重新查询，请勿重复提交。');
    },
    [onUnauthorized],
  );
  useEffect(() => {
    let disposed = false;
    const refresh = () => {
      if (document.hidden) return;
      void client
        .updates()
        .then((value) => {
          if (!disposed) {
            setStatus(value);
            setError('');
          }
        })
        .catch((error) => {
          if (!disposed) failure(error);
        });
    };
    refresh();
    const timer = setInterval(refresh, active ? 5000 : 30000);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      disposed = true;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [client, failure, active]);
  const check = async () => {
    setBusy(true);
    try {
      setStatus(await client.checkUpdates());
      setError('');
    } catch (error) {
      failure(error);
    } finally {
      setBusy(false);
    }
  };
  const upgrade = async () => {
    const version = confirm;
    setConfirm('');
    setBusy(true);
    try {
      setStatus(await client.upgrade(version));
      setError('');
    } catch (error) {
      failure(error);
    } finally {
      setBusy(false);
    }
  };
  if (!status?.enabled && !error) return null;
  const execution = status?.execution;
  return (
    <section aria-label="平台更新" className="platform-updates">
      <div className="pending-actions">
        <strong>平台版本 {status?.current || '查询中'}</strong>
        {status?.candidate && <span>发现新版本 {status.candidate.version}</span>}
        <Button
          size="small"
          variant="outline"
          disabled={busy || active}
          onClick={() => void check()}
        >
          检查更新
        </Button>
        {status?.candidate && (
          <Button
            size="small"
            disabled={busy || active || execution?.phase === 'interrupted'}
            onClick={() => setConfirm(status.candidate!.version)}
          >
            升级到 {status.candidate.version}
          </Button>
        )}
        {execution?.phase === 'succeeded' && (
          <Button size="small" variant="text" onClick={() => window.location.reload()}>
            刷新页面
          </Button>
        )}
      </div>
      {status?.checked_at && (
        <p className="muted">
          上次检查：{new Date(status.checked_at).toLocaleString('zh-CN')} ·
          通过发布通知和启动补查发现正式版，仅手动升级
        </p>
      )}
      {status?.enabled && (
        <p className="muted">
          发布通知：{status.notifications.connected ? '已连接' : '未连接'} · dev 自动升级：
          {status.notifications.dev_auto_upgrade ? '开启' : '关闭'}
          {status.notifications.dev_tag && ` · 收到 ${status.notifications.dev_tag} 通知`}
        </p>
      )}
      {status?.notifications.last_attempt?.phase === 'failed' && (
        <Alert
          theme="warning"
          message="上次 dev 自动升级未完成，该构建不会自动重试，请查看日志并手动处理。"
        />
      )}
      {status?.notifications.error && (
        <Alert theme="warning" message={status.notifications.error} />
      )}
      {execution && (
        <Alert
          theme={
            execution.phase === 'succeeded'
              ? 'success'
              : execution.phase === 'failed' || execution.phase === 'interrupted'
                ? 'error'
                : 'info'
          }
          title={`升级 ${execution.version} · ${phases[execution.phase] || '状态待确认'}`}
          message={execution.message}
        />
      )}
      {(error || status?.error) && (
        <Alert
          theme="warning"
          message={error || '检查更新失败，请检查服务器网络、仓库权限和发布资产。'}
        />
      )}
      <Modal
        label="升级平台"
        header={`升级平台到 ${confirm}`}
        visible={!!confirm}
        onClose={() => setConfirm('')}
        onConfirm={() => void upgrade()}
        confirmBtn={{ content: '确认升级' }}
      >
        <p>将下载并校验安装包，等待部署任务完成，再执行兼容性检查、备份和升级后验证。</p>
        <p>
          已有应用容器继续运行，管理页面和公网入口可能短暂中断。请先配置业务验证接口，并选择合适的维护时间。
        </p>
        <p>此次操作更新平台，不更新业务应用镜像。验证失败时，安装器将按兼容性条件尝试回退。</p>
      </Modal>
    </section>
  );
}
