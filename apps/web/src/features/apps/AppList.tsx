import { Button } from '../../components/Button';
import { useMemo, useState } from 'react';
import { Input, Table, Tag, Pagination, type PrimaryTableCol } from 'tdesign-react';
import { SearchIcon } from 'tdesign-icons-react';
import type { Application } from '@deploy-platform/control-client';
import { StatusTag } from '../../components/StatusTag';
interface Props {
  apps: Application[];
  loading: boolean;
  disabled: boolean;
  onDiagnostics: (app: Application) => void;
  onIntent: (app: Application, action: 'start' | 'stop') => void;
}
export default function AppList({ apps, loading, disabled, onIntent, onDiagnostics }: Props) {
  const [search, setSearch] = useState('');
  const [mobilePage, setMobilePage] = useState(1);
  const filtered = useMemo(
    () =>
      apps.filter((a) => `${a.app.project}/${a.app.name}`.includes(search.trim().toLowerCase())),
    [apps, search],
  );
  const currentMobilePage = Math.min(mobilePage, Math.max(1, Math.ceil(filtered.length / 10)));
  const columns: PrimaryTableCol<Application>[] = [
    {
      colKey: 'app',
      title: '应用 / 项目',
      minWidth: 220,
      cell: ({ row }) => (
        <div className="app-cell">
          <span className="app-symbol">{row.app.name.slice(0, 2).toUpperCase()}</span>
          <div>
            <strong>{row.app.name}</strong>
            <small>{row.app.project}</small>
          </div>
        </div>
      ),
    },
    {
      colKey: 'phase',
      title: '应用阶段',
      minWidth: 160,
      cell: ({ row }) => <StatusTag value={row.phase} application />,
    },
    {
      colKey: 'desired',
      title: '期望状态',
      minWidth: 100,
      cell: ({ row }) => (
        <span className="intent">
          <i className={row.desired === 'running' ? 'green' : 'gray'} />
          {row.desired === 'running' ? '运行' : '停止'}
        </span>
      ),
    },
    {
      colKey: 'instance_id',
      title: '实例',
      minWidth: 170,
      cell: ({ row }) => (
        <div>
          <code title={row.instance_id ?? ''}>
            {row.instance_id ? row.instance_id.slice(0, 12) : '—'}
          </code>
          {row.recovery.attempts > 0 && (
            <small className={row.recovery.error_code ? 'error-text' : 'muted'}>
              恢复尝试 {row.recovery.attempts} 次
            </small>
          )}
        </div>
      ),
    },
    {
      colKey: 'actions',
      title: '管理',
      width: 230,
      fixed: 'right',
      cell: ({ row }) => (
        <div className="row-actions">
          <Button variant="text" size="small" onClick={() => onDiagnostics(row)}>
            诊断
          </Button>
          <Button
            variant="text"
            size="small"
            disabled={disabled}
            onClick={() => onIntent(row, 'start')}
          >
            启动
          </Button>
          <Button
            variant="text"
            theme="danger"
            size="small"
            disabled={disabled}
            onClick={() => onIntent(row, 'stop')}
          >
            停止
          </Button>
        </div>
      ),
    },
  ];
  return (
    <section className="panel">
      <div className="table-toolbar">
        <div>
          <h3>
            全部应用{' '}
            <Tag size="small" shape="round">
              {apps.length}
            </Tag>
          </h3>
          <p>每个应用独立部署，运行意图由节点持续维护。</p>
        </div>
        <Input
          value={search}
          onChange={(value) => {
            setSearch(value);
            setMobilePage(1);
          }}
          prefixIcon={<SearchIcon />}
          placeholder="搜索应用或项目"
          clearable
          style={{ width: 260 }}
          aria-label="搜索应用或项目"
        />
      </div>
      <div className="desktop-app-table">
        <Table
          rowKey="fingerprint"
          data={filtered}
          columns={columns}
          loading={loading}
          hover
          size="large"
          tableLayout="fixed"
          empty={search ? '没有找到匹配的应用' : '还没有部署应用。点击「部署应用」开始。'}
          pagination={
            filtered.length > 10 ? { defaultPageSize: 10, total: filtered.length } : undefined
          }
        />
      </div>
      <div className="mobile-app-list">
        {filtered.slice((currentMobilePage - 1) * 10, currentMobilePage * 10).map((app) => (
          <section
            key={app.fingerprint}
            className="mobile-app-card"
            aria-label={`${app.app.project}/${app.app.name}`}
          >
            <div className="mobile-card-header">
              <div className="app-cell">
                <span className="app-symbol">{app.app.name.slice(0, 2).toUpperCase()}</span>
                <div>
                  <strong>{app.app.name}</strong>
                  <small>{app.app.project}</small>
                </div>
              </div>
              <StatusTag value={app.phase} application />
            </div>
            <div className="mobile-card-meta">
              <span>期望状态</span>
              <span className="intent">
                <i className={app.desired === 'running' ? 'green' : 'gray'} />
                {app.desired === 'running' ? '运行' : '停止'}
              </span>
            </div>
            <div className="mobile-card-meta">
              <span>实例</span>
              <code>{app.instance_id?.slice(0, 12) ?? '—'}</code>
            </div>
            <div className="mobile-card-actions">
              <Button variant="outline" onClick={() => onDiagnostics(app)}>
                诊断
              </Button>
              <Button variant="outline" disabled={disabled} onClick={() => onIntent(app, 'start')}>
                启动
              </Button>
              <Button
                variant="outline"
                theme="danger"
                disabled={disabled}
                onClick={() => onIntent(app, 'stop')}
              >
                停止
              </Button>
            </div>
          </section>
        ))}
        {filtered.length === 0 && (
          <p className="mobile-empty">
            {loading
              ? '正在读取应用…'
              : search
                ? '未找到应用，请调整搜索。'
                : '尚无应用，点击部署应用开始。'}
          </p>
        )}
        {filtered.length > 10 && (
          <Pagination
            size="small"
            current={currentMobilePage}
            pageSize={10}
            total={filtered.length}
            showPageSize={false}
            showJumper={false}
            onChange={(page) => setMobilePage(page.current)}
          />
        )}
      </div>
      <div className="panel-foot">阶段来自最近一次节点观察；进程启动不代表业务已就绪。</div>
    </section>
  );
}
