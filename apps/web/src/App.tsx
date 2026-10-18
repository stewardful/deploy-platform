import { Button } from './components/Button';
import { lazy, Suspense, useCallback, useMemo, useState } from 'react';
import { Alert, Loading } from 'tdesign-react';
import {
  AddIcon,
  AppIcon,
  TaskIcon,
  ServerIcon,
  LogoutIcon,
  RefreshIcon,
  ChevronRightIcon,
} from 'tdesign-icons-react';
import {
  ControlClient,
  ControlError,
  requestID,
  type Application,
  type AppSpec,
} from '@deploy-platform/control-client';
import AppList from './features/apps/AppList';
import { PlatformUpdates } from './features/updates/PlatformUpdates';
import TaskList from './features/jobs/TaskList';
import { Modal } from './components/Modal';
import { Login } from './components/Login';
import { TaskDetails } from './components/TaskDetails';
import { useApplications } from './hooks/useApplications';
import { useTasks } from './hooks/useTasks';
const IngressPanel = lazy(() => import('./features/ingress/IngressPanel'));
const DiagnosticsPanel = lazy(() => import('./features/apps/DiagnosticsPanel'));
const ResourcesPanel = lazy(() => import('./features/apps/ResourcesPanel'));
const DeployDialog = lazy(() => import('./features/apps/DeployDialog'));
export default function App() {
  const [token, setToken] = useState('');
  const [message, setMessage] = useState('');
  const logout = useCallback(() => {
    setToken('');
    setMessage('会话已结束，请重新输入管理令牌。');
  }, []);
  if (!token)
    return (
      <Login
        message={message}
        onLogin={(value) => {
          setMessage('');
          setToken(value);
        }}
      />
    );
  return <Workspace key={token} token={token} onLogout={logout} />;
}
function Workspace({ token, onLogout }: { token: string; onLogout: () => void }) {
  const client = useMemo(() => new ControlClient(token), [token]);
  const { apps, loading, error, updated, refresh } = useApplications(client, onLogout);
  const settled = useCallback(() => {
    void refresh();
  }, [refresh]);
  const tasks = useTasks(client, onLogout, settled);
  const [page, setPage] = useState<'apps' | 'tasks' | 'resources' | 'ingress'>('apps');
  const [diagnosticApp, setDiagnosticApp] = useState<Application | null>(null);
  const [deploy, setDeploy] = useState(false);
  const [intent, setIntent] = useState<{ app: Application; action: 'start' | 'stop' } | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [notice, setNotice] = useState('');
  const [discardConfirm, setDiscardConfirm] = useState(false);
  const disabled = tasks.sending || !!tasks.pending || tasks.damaged;
  const accepted = (id: number) => {
    setSelected(id);
    setNotice(`任务 #${id} 已提交，可以在任务中心查看执行结果。`);
  };
  const apply = async (spec: AppSpec) => {
    const job = await tasks.submit({
      path: '/api/v1/apps/apply',
      body: { request_id: requestID(), spec, startup_wait_ms: 1000 },
      summary: `部署 ${spec.app.project}/${spec.app.name}`,
    });
    setDeploy(false);
    if (job) {
      setDeploy(false);
      accepted(job.id);
      return true;
    }
    return false;
  };
  const executeIntent = async () => {
    if (!intent) return;
    const { app, action } = intent;
    setIntent(null);
    const job = await tasks.submit({
      path: `/api/v1/apps/${app.app.project}/${app.app.name}/${action}`,
      body: { request_id: requestID() },
      summary: `${action === 'start' ? '启动' : '停止'} ${app.app.project}/${app.app.name}`,
    });
    if (job) accepted(job.id);
  };
  const query = async (id: string) => {
    try {
      const job = await tasks.fetchJob(id);
      setSelected(job.id);
    } catch (error) {
      if (error instanceof ControlError && error.status === 401) onLogout();
      throw error;
    }
  };
  const running = apps.filter((a) => a.desired === 'running').length;
  return (
    <div className="workspace">
      {diagnosticApp && (
        <Suspense fallback={<Loading loading />}>
          <DiagnosticsPanel
            client={client}
            app={diagnosticApp.app}
            onClose={() => setDiagnosticApp(null)}
            onUnauthorized={onLogout}
          />
        </Suspense>
      )}
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <ServerIcon />
          </span>
          <span>
            Cloud<span className="brand-dot">.</span>
          </span>
        </div>
        <div className="sidebar-caption">工作空间</div>
        <nav aria-label="主导航">
          <button
            className={page === 'apps' ? 'nav-item active' : 'nav-item'}
            onClick={() => setPage('apps')}
          >
            <AppIcon />
            应用管理<span>{apps.length}</span>
          </button>
          <button
            className={page === 'tasks' ? 'nav-item active' : 'nav-item'}
            onClick={() => setPage('tasks')}
          >
            <TaskIcon />
            任务中心
          </button>
          <button
            className={page === 'resources' ? 'nav-item active' : 'nav-item'}
            onClick={() => setPage('resources')}
          >
            <ServerIcon />
            数据与密钥
          </button>
          <button
            className={page === 'ingress' ? 'nav-item active' : 'nav-item'}
            onClick={() => setPage('ingress')}
          >
            <ServerIcon />
            域名与 HTTPS
          </button>
        </nav>
        <div className="sidebar-bottom">
          <div className="node-avatar">
            <ServerIcon />
          </div>
          <div>
            <strong>当前节点</strong>
            <small>独立工作台</small>
          </div>
          <span className="node-dot" />
        </div>
      </aside>
      <div className="workspace-main">
        <header className="topbar">
          <div className="breadcrumbs">
            工作空间
            <ChevronRightIcon />
            <strong>
              {page === 'apps'
                ? '应用管理'
                : page === 'tasks'
                  ? '任务中心'
                  : page === 'ingress'
                    ? '域名与 HTTPS'
                    : '数据与密钥'}
            </strong>
          </div>
          <div className="topbar-actions">
            <span className="node-badge">
              <i className={error ? 'gray' : 'green'} />
              {error ? '连接待恢复' : '当前节点'}
            </span>
            <Button variant="text" icon={<LogoutIcon />} onClick={onLogout}>
              退出
            </Button>
          </div>
        </header>
        <main className="page-content">
          <PlatformUpdates client={client} onUnauthorized={onLogout} />
          <div className="page-heading">
            <div>
              <div className="eyebrow">PROJECT WORKSPACE</div>
              <h1>
                {page === 'apps'
                  ? '应用管理'
                  : page === 'tasks'
                    ? '任务中心'
                    : page === 'ingress'
                      ? '域名与 HTTPS'
                      : '数据与密钥'}
              </h1>
              <p>
                {page === 'apps'
                  ? '从部署到运行，在这里管理你的项目。'
                  : page === 'tasks'
                    ? '追踪每一次操作，查看当前进度和执行结果。'
                    : page === 'ingress'
                      ? '配置项目的公网入口和自动 HTTPS。'
                      : '保留项目数据，以文件引用敏感配置。'}
              </p>
            </div>
            <div className="page-actions">
              {page === 'apps' && (
                <>
                  <Button
                    variant="outline"
                    icon={<RefreshIcon />}
                    loading={loading}
                    onClick={() => void refresh()}
                  >
                    刷新
                  </Button>
                  <Button icon={<AddIcon />} disabled={disabled} onClick={() => setDeploy(true)}>
                    部署应用
                  </Button>
                </>
              )}
            </div>
          </div>
          {page === 'ingress' && (
            <Suspense fallback={<Loading loading />}>
              <IngressPanel client={client} apps={apps} onUnauthorized={onLogout} />
            </Suspense>
          )}
          {error && <Alert theme="error" title="应用列表暂时不可用" message={error} />}
          {notice && <Alert theme="success" message={notice} close onClose={() => setNotice('')} />}
          {(tasks.pending || tasks.damaged) && (
            <Alert
              theme="warning"
              title={tasks.damaged ? '本地提交记录无法读取' : '有一笔提交尚未确认'}
              message={
                <div className="pending-message">
                  {tasks.pending && (
                    <>
                      <p>{tasks.pending.summary}</p>
                      <code>请求标识：{tasks.pending.body.request_id}</code>
                    </>
                  )}
                  <p>请先确认原操作，避免重复提交。清除本地记录不会取消服务端可能已接受的任务。</p>
                  <div className="pending-actions">
                    {tasks.pending && (
                      <Button
                        size="small"
                        loading={tasks.sending}
                        onClick={() =>
                          void tasks.retry().then((job) => {
                            if (job) accepted(job.id);
                          })
                        }
                      >
                        使用原请求确认
                      </Button>
                    )}
                    <Button
                      size="small"
                      variant="text"
                      theme="danger"
                      disabled={tasks.sending}
                      onClick={() => setDiscardConfirm(true)}
                    >
                      清除本地记录
                    </Button>
                  </div>
                </div>
              }
            />
          )}
          {tasks.error && (
            <div role="alert">
              <Alert theme="error" message={tasks.error} />
            </div>
          )}
          {page === 'resources' ? (
            <Suspense fallback={<Loading />}>
              <ResourcesPanel client={client} onLogout={onLogout} />
            </Suspense>
          ) : page === 'apps' ? (
            <>
              <section className="summary-grid" aria-label="应用统计">
                <div className="summary-card">
                  <span className="summary-icon blue">
                    <AppIcon />
                  </span>
                  <div>
                    <small>全部应用</small>
                    <strong>{updated ? apps.length : '—'}</strong>
                  </div>
                  <span className="summary-note">已部署到节点</span>
                </div>
                <div className="summary-card">
                  <span className="summary-icon green-bg">
                    <span className="large-dot" />
                  </span>
                  <div>
                    <small>期望运行</small>
                    <strong>{updated ? running : '—'}</strong>
                  </div>
                  <span className="summary-note">节点自动维护</span>
                </div>
                <div className="summary-card">
                  <span className="summary-icon gray-bg">
                    <span className="pause-symbol">Ⅱ</span>
                  </span>
                  <div>
                    <small>期望停止</small>
                    <strong>{updated ? apps.length - running : '—'}</strong>
                  </div>
                  <span className="summary-note">保留部署配置</span>
                </div>
              </section>
              <AppList
                onDiagnostics={setDiagnosticApp}
                apps={apps}
                loading={loading}
                disabled={disabled}
                onIntent={(app, action) => setIntent({ app, action })}
              />
              <div className="update-note">
                {updated
                  ? `上次更新 ${updated.toLocaleTimeString('zh-CN')} · 页面可见时自动刷新`
                  : '正在读取节点状态…'}
              </div>
            </>
          ) : page === 'tasks' ? (
            <TaskList
              ids={tasks.ids}
              jobs={tasks.jobs}
              errors={tasks.errors}
              onQuery={query}
              onSelect={setSelected}
            />
          ) : null}
          <footer className="page-footer">
            <span>Cloud · 项目管理工作台</span>
            <span>单节点 / 本地连接</span>
          </footer>
        </main>
      </div>
      <Suspense fallback={<Loading fullscreen />}>
        <DeployDialog
          visible={deploy}
          busy={tasks.sending}
          onClose={() => setDeploy(false)}
          onSubmit={apply}
        />
      </Suspense>
      <Modal
        label={intent?.action === 'stop' ? '停止应用' : '启动应用'}
        visible={!!intent}
        header={intent?.action === 'stop' ? '停止应用' : '启动应用'}
        onClose={() => setIntent(null)}
        onConfirm={() => void executeIntent()}
        confirmBtn={{
          content: intent?.action === 'stop' ? '停止应用' : '启动应用',
          theme: intent?.action === 'stop' ? 'danger' : 'primary',
        }}
      >
        <p>
          确认{intent?.action === 'stop' ? '停止' : '启动'}{' '}
          <strong>
            {intent?.app.app.project}/{intent?.app.app.name}
          </strong>
          ？
        </p>
        <p className="muted">
          {intent?.action === 'stop'
            ? '停止后会保留配置，节点不会自动拉起此应用。'
            : '提交运行意图，节点将尝试拉起应用。'}
        </p>
      </Modal>
      <Modal
        label="清除本地提交记录"
        visible={discardConfirm}
        header="清除本地提交记录"
        onClose={() => setDiscardConfirm(false)}
        onConfirm={() => {
          tasks.discard();
          setDiscardConfirm(false);
        }}
        confirmBtn={{ content: '清除本地记录', theme: 'danger' }}
      >
        <p>
          这只清除本标签页的记录，不会取消服务端任务。建议先使用原请求确认，或查询已知任务
          ID，避免重复操作。
        </p>
      </Modal>
      <TaskDetails
        id={selected}
        job={selected ? tasks.jobs[selected] : undefined}
        error={selected ? tasks.errors[selected] : undefined}
        onClose={() => setSelected(null)}
      />
    </div>
  );
}
