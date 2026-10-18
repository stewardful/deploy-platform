import { Button } from '../../components/Button';
import { useState } from 'react';
import { Input, Table, Alert, type PrimaryTableCol } from 'tdesign-react';
import { ControlError, type Job } from '@deploy-platform/control-client';
import { StatusTag } from '../../components/StatusTag';
export default function TaskList({
  ids,
  jobs,
  errors,
  onQuery,
  onSelect,
}: {
  ids: number[];
  jobs: Record<number, Job>;
  errors: Record<number, string>;
  onQuery: (id: string) => Promise<void>;
  onSelect: (id: number) => void;
}) {
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const lookup = async () => {
    setBusy(true);
    setError('');
    try {
      await onQuery(query.trim());
    } catch (error) {
      setError(
        error instanceof ControlError && error.status === 404
          ? '未找到该任务，请核对任务 ID。'
          : error instanceof Error
            ? error.message
            : '查询失败',
      );
    } finally {
      setBusy(false);
    }
  };
  const columns: PrimaryTableCol<{ id: number }>[] = [
    {
      colKey: 'id',
      title: '任务 ID',
      width: 100,
      cell: ({ row }) => <strong className="mono">#{row.id}</strong>,
    },
    {
      colKey: 'request',
      title: '请求标识',
      minWidth: 260,
      cell: ({ row }) => <code>{jobs[row.id]?.request_id ?? '正在读取…'}</code>,
    },
    {
      colKey: 'status',
      title: '任务状态',
      minWidth: 150,
      cell: ({ row }) =>
        jobs[row.id] ? (
          <StatusTag value={jobs[row.id].state} />
        ) : (
          <span className="muted">{errors[row.id] ? '读取失败' : '等待查询'}</span>
        ),
    },
    {
      colKey: 'result',
      title: '结果',
      minWidth: 180,
      cell: ({ row }) => (
        <span className={jobs[row.id]?.error ? 'error-text' : 'muted'}>
          {jobs[row.id]?.error?.message ??
            errors[row.id] ??
            (jobs[row.id]?.state === 'succeeded' ? '操作已完成' : '—')}
        </span>
      ),
    },
    {
      colKey: 'action',
      title: '',
      width: 100,
      cell: ({ row }) => (
        <Button variant="text" size="small" onClick={() => onSelect(row.id)}>
          查看详情
        </Button>
      ),
    },
  ];
  return (
    <section className="panel">
      <div className="table-toolbar">
        <div>
          <h3>任务记录</h3>
          <p>显示本标签页最近 50 个任务，也可以按 ID 查询历史任务。</p>
        </div>
        <form
          className="query-form"
          onSubmit={(event) => {
            event.preventDefault();
            void lookup();
          }}
        >
          <Input
            value={query}
            onChange={setQuery}
            placeholder="输入任务 ID"
            aria-label="任务 ID"
            style={{ width: 180 }}
          />
          <Button type="submit" variant="outline" loading={busy}>
            查询任务
          </Button>
        </form>
      </div>
      {error && (
        <div className="inline-alert">
          <Alert theme="error" message={error} />
        </div>
      )}
      <Table
        rowKey="id"
        data={ids.map((id) => ({ id }))}
        columns={columns}
        hover
        size="large"
        empty="暂无任务，部署或启停应用后会在这里显示。"
        pagination={ids.length > 10 ? { defaultPageSize: 10, total: ids.length } : undefined}
      />
      <div className="panel-foot">活动任务按需轮询；离开页面不会取消已提交的操作。</div>
    </section>
  );
}
