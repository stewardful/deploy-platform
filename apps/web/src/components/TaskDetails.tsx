import { Alert } from 'tdesign-react';
import type { Job } from '@deploy-platform/control-client';
import { Modal } from './Modal';
import { StatusTag } from './StatusTag';
export function TaskDetails({
  id,
  job,
  error,
  onClose,
}: {
  id: number | null;
  job?: Job;
  error?: string;
  onClose: () => void;
}) {
  return (
    <Modal
      label={`任务 #${id ?? ''}`}
      visible={id !== null}
      header={`任务 #${id ?? ''}`}
      footer={false}
      onClose={onClose}
      width={640}
    >
      {job ? (
        <>
          <div className="detail-heading">
            <StatusTag value={job.state} />
            <code>{job.request_id}</code>
          </div>
          {job.error && <Alert theme="error" title="任务执行失败" message={job.error.message} />}
          <dl className="detail-meta">
            <dt>任务 ID</dt>
            <dd>{job.id}</dd>
            <dt>请求标识</dt>
            <dd className="mono">{job.request_id}</dd>
          </dl>
          {job.result && (
            <>
              <h4>执行结果</h4>
              <pre className="result-json">{JSON.stringify(job.result, null, 2)}</pre>
            </>
          )}
          {!['succeeded', 'failed'].includes(job.state) && (
            <p className="muted">任务正在处理，状态会自动更新。关闭此窗口不会取消操作。</p>
          )}
          {job.state === 'failed' && (
            <p className="muted">
              失败任务保留原结果。重新操作前请查看应用状态，运行意图可能已被自动恢复处理。
            </p>
          )}
        </>
      ) : (
        <Alert theme={error ? 'error' : 'info'} message={error ?? '正在查询任务…'} />
      )}
    </Modal>
  );
}
