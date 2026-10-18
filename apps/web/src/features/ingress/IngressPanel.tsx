import { useEffect, useState, useCallback } from 'react';
import { Alert, Input, InputNumber, Select, Tag } from 'tdesign-react';
import {
  type Application,
  type ControlClient,
  type DomainRoute,
  type RouteStatus,
  type IngressSnapshot,
  type CertificateStatus,
  validDomain,
  ControlError,
} from '@deploy-platform/control-client';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
const empty: DomainRoute = {
  domain: '',
  app: { project: '', name: '' },
  port: 8080,
  tls: 'production',
  enabled: true,
};
const certificateNames: Record<string, string> = {
  valid: '证书已就绪（系统信任）',
  pending: '尚未获取到证书',
  staging: '测试证书（浏览器不信任）',
  untrusted: '证书存在，但系统不信任',
  hostname_mismatch: '证书域名不匹配',
  expired_or_not_yet_valid: '证书不在有效期内',
};
export default function IngressPanel({
  client,
  apps,
  onUnauthorized,
}: {
  client: ControlClient;
  apps: Application[];
  onUnauthorized: () => void;
}) {
  const [snapshot, setSnapshot] = useState<IngressSnapshot | null>(null),
    [error, setError] = useState(''),
    [notice, setNotice] = useState('');
  const [edit, setEdit] = useState<{ route: DomainRoute; revision: number } | null>(null),
    [remove, setRemove] = useState<RouteStatus | null>(null);
  const [busy, setBusy] = useState(false),
    [certificate, setCertificate] = useState<CertificateStatus | null>(null),
    [checking, setChecking] = useState('');
  const problem = useCallback(
    (e: unknown) => {
      if (e instanceof ControlError && e.status === 401) {
        onUnauthorized();
        return;
      }
      setError(e instanceof Error ? e.message : '操作失败，请刷新确认服务端状态后再试。');
    },
    [onUnauthorized],
  );
  const refresh = useCallback(async () => {
    try {
      setSnapshot(await client.routes());
      setError('');
    } catch (e) {
      problem(e);
    }
  }, [client, problem]);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => {
      if (!document.hidden) void refresh();
    }, 10000);
    return () => clearInterval(timer);
  }, [refresh]);
  const save = async () => {
    if (!edit) return;
    if (!validDomain(edit.route.domain)) {
      setError('请输入小写公网域名，不包含协议、端口、路径或通配符；国际化域名请使用 punycode。');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await client.changeRoute(edit.route, edit.revision);
      setEdit(null);
      setNotice('配置已保存，等待代理同步和证书签发。');
      await refresh();
    } catch (e) {
      problem(e);
    } finally {
      setBusy(false);
    }
  };
  const mutate = async (item: RouteStatus) => {
    setBusy(true);
    try {
      await client.changeRoute({ ...item.route, enabled: !item.route.enabled }, item.revision);
      setNotice('运行意图已保存，等待代理同步。');
      await refresh();
    } catch (e) {
      problem(e);
    } finally {
      setBusy(false);
    }
  };
  const check = async (domain: string) => {
    setChecking(domain);
    try {
      setCertificate(await client.certificate(domain));
    } catch (e) {
      problem(e);
    } finally {
      setChecking('');
    }
  };
  return (
    <section className="panel ingress-panel">
      <div className="table-toolbar">
        <div>
          <h3>公网域名</h3>
          <p>HTTPS → 反向代理 → 项目内应用，不需要映射宿主端口。</p>
        </div>
        <div className="row-actions">
          <Button variant="outline" onClick={() => void refresh()}>
            刷新域名
          </Button>
          <Button
            onClick={() => {
              setError('');
              setEdit({ route: { ...empty, app: apps[0]?.app ?? empty.app }, revision: 0 });
            }}
          >
            绑定域名
          </Button>
        </div>
      </div>
      <div className="ingress-content">
        <Alert
          theme="info"
          message="先将域名 A/AAAA 解析到此服务器，在腾讯云安全组及主机防火墙放行 TCP 80、443。应用需要使用项目网络。证书签发和续期由节点自动处理；配置生效与证书就绪是两个状态。"
        />
        {error && <Alert theme="error" message={error} />}
        {notice && <Alert theme="success" message={notice} close onClose={() => setNotice('')} />}
        {snapshot && (snapshot.error_code || snapshot.revision !== snapshot.applied_revision) && (
          <Alert
            theme="warning"
            message={
              snapshot.error_code
                ? '代理同步失败，正在自动重试。检查代理服务和 80/443 端口占用。'
                : '有域名配置等待生效；移除或停用以代理同步完成为准。'
            }
          />
        )}
        {!snapshot?.routes.length && (
          <p className="mobile-empty">还没有域名绑定。先部署应用，再绑定公网域名。</p>
        )}
        {snapshot?.routes.map((item) => (
          <article className="domain-card" key={item.route.domain}>
            <div>
              <h4>
                <a href={`https://${item.route.domain}`} target="_blank" rel="noreferrer">
                  {item.route.domain}
                </a>
              </h4>
              <p>
                {item.route.app.project} / {item.route.app.name} · 容器端口 {item.route.port}
              </p>
              <div className="row-actions">
                <Tag theme={item.proxy_state === 'applied' ? 'success' : 'warning'}>
                  {item.proxy_state === 'applied'
                    ? '代理已同步'
                    : item.proxy_state === 'failed'
                      ? '代理同步失败'
                      : '等待同步'}
                </Tag>
                <Tag>
                  {item.route.enabled
                    ? item.upstream_ready
                      ? '上游运行'
                      : '上游未就绪'
                    : '已停用'}
                </Tag>
                <Tag theme={item.route.tls === 'staging' ? 'warning' : 'default'}>
                  {item.route.tls === 'staging' ? '测试 HTTPS' : '正式 HTTPS'}
                </Tag>
              </div>
            </div>
            <div className="row-actions">
              <Button
                variant="text"
                disabled={!item.route.enabled || !!checking}
                loading={checking === item.route.domain}
                onClick={() => void check(item.route.domain)}
              >
                查看证书
              </Button>
              <Button
                variant="text"
                disabled={busy}
                onClick={() =>
                  setEdit({ route: structuredClone(item.route), revision: item.revision })
                }
              >
                编辑
              </Button>
              <Button variant="text" disabled={busy} onClick={() => void mutate(item)}>
                {item.route.enabled ? '停用' : '启用'}
              </Button>
              <Button variant="text" theme="danger" disabled={busy} onClick={() => setRemove(item)}>
                移除
              </Button>
            </div>
          </article>
        ))}
      </div>
      {edit && (
        <Modal
          visible
          label="域名绑定"
          header={edit.revision ? '编辑域名绑定' : '绑定域名'}
          width={600}
          closeDisabled={busy}
          onClose={() => setEdit(null)}
          confirmBtn={{ content: '保存绑定', loading: busy }}
          cancelBtn={{ content: '取消', disabled: busy }}
          onConfirm={() => void save()}
        >
          {error && <Alert theme="error" message={error} />}
          <label className="form-field">
            公网域名
            <Input
              value={edit.route.domain}
              disabled={edit.revision > 0}
              placeholder="例如 app.your-domain.com"
              onChange={(domain) => setEdit({ ...edit, route: { ...edit.route, domain } })}
            />
          </label>
          <label className="form-field">
            目标应用
            <Select
              value={`${edit.route.app.project}/${edit.route.app.name}`}
              options={apps.map((a) => ({
                label: `${a.app.project} / ${a.app.name}`,
                value: `${a.app.project}/${a.app.name}`,
              }))}
              onChange={(value) => {
                const [project, name] = String(value).split('/');
                setEdit({ ...edit, route: { ...edit.route, app: { project, name } } });
              }}
            />
          </label>
          <label className="form-field">
            容器 HTTP 端口
            <InputNumber
              theme="normal"
              min={1}
              max={65535}
              value={edit.route.port}
              onChange={(v) => setEdit({ ...edit, route: { ...edit.route, port: Number(v) } })}
            />
          </label>
          <label className="form-field">
            HTTPS 环境
            <Select
              value={edit.route.tls}
              options={[
                { label: '正式证书（公网使用）', value: 'production' },
                { label: '测试证书（浏览器不信任）', value: 'staging' },
              ]}
              onChange={(v) =>
                setEdit({ ...edit, route: { ...edit.route, tls: v as DomainRoute['tls'] } })
              }
            />
          </label>
          <p className="muted">
            保留原请求路径，支持 WebSocket。前后端可分别绑定不同域名；同域名的 /api
            转发可继续由前端项目处理。
          </p>
        </Modal>
      )}
      {remove && (
        <Modal
          visible
          label="移除域名"
          header="移除域名绑定"
          onClose={() => setRemove(null)}
          confirmBtn={{ content: '移除绑定', theme: 'danger', loading: busy }}
          onConfirm={() => {
            setBusy(true);
            void client
              .deleteRoute(remove.route.domain, remove.revision)
              .then(async () => {
                setRemove(null);
                setNotice('移除已保存，等待代理同步。证书缓存会保留。');
                await refresh();
              })
              .catch(problem)
              .finally(() => setBusy(false));
          }}
        >
          {error && <Alert theme="error" message={error} />}
          <p>移除 {remove.route.domain} 的公网路由，应用和数据继续保留。</p>
        </Modal>
      )}
      {certificate && (
        <Modal
          visible
          label="HTTPS 证书"
          header={certificate.domain}
          footer={false}
          onClose={() => setCertificate(null)}
        >
          {error && <Alert theme="error" message={error} />}
          <h4>{certificateNames[certificate.state] ?? certificate.state}</h4>
          <p>
            到期：{certificate.not_after ? new Date(certificate.not_after).toLocaleString() : '—'}
          </p>
          <p>签发者：{certificate.issuer ?? '—'}</p>
          <p>检查于：{new Date(certificate.checked_at_ms).toLocaleString()}</p>
          <p className="muted">
            这是本机代理实际提供的证书快照。未就绪时检查 DNS、80/443
            入站、服务器出站和代理日志；首次签发可能需要数分钟。
          </p>
          <Button
            variant="outline"
            loading={!!checking}
            onClick={() => void check(certificate.domain)}
          >
            重新检查证书
          </Button>
        </Modal>
      )}
    </section>
  );
}
