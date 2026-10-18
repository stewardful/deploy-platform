import { useState } from 'react';
import { Alert, Input, Textarea, Tag, Select } from 'tdesign-react';
import { Button } from '../../components/Button';
import {
  ControlClient,
  ControlError,
  type SecretInfo,
  type VolumeInfo,
} from '@deploy-platform/control-client';

export default function ResourcesPanel({
  client,
  onLogout,
}: {
  client: ControlClient;
  onLogout: () => void;
}) {
  const [project, setProject] = useState('default');
  const [app, setApp] = useState('');
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [format, setFormat] = useState('raw');
  const [multiline, setMultiline] = useState(false);
  const [secrets, setSecrets] = useState<SecretInfo[]>([]);
  const [volumes, setVolumes] = useState<VolumeInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const failure = (error: unknown) => {
    if (error instanceof ControlError && error.status === 401) onLogout();
    setNotice(error instanceof Error ? error.message : '资源请求失败');
  };
  const load = async () => {
    setBusy(true);
    setNotice('');
    setSecrets([]);
    setVolumes([]);
    try {
      setSecrets(await client.secrets(project));
      if (app) setVolumes(await client.volumes({ project, name: app }));
    } catch (e) {
      failure(e);
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    setBusy(true);
    setNotice('');
    // Secret bytes stay only in component memory and this write-only request. Never useTasks/sessionStorage.
    if (format === 'redis' && /[\x00-\x1f\x7f]/.test(value)) {
      setNotice('Redis 密码不能含控制字符。');
      setBusy(false);
      return;
    }
    const input = {
      project,
      name,
      value: format === 'redis' ? `requirepass ${JSON.stringify(value)}\n` : value,
    };
    setValue('');
    try {
      await client.createSecret(input);
      setNotice('密钥已保存。只返回名称，不支持读取、覆盖或自动轮换。');
      setSecrets(await client.secrets(project));
    } catch (e) {
      failure(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="resource-panel">
      <h2>资源配置</h2>
      <p>数据卷属于具体应用，停止或重建容器都保留数据。密钥属于项目，通过只读文件挂载。</p>
      <div className="form-grid">
        <label>
          项目名称
          <Input
            value={project}
            disabled={busy}
            onChange={(v) => {
              setProject(v);
              setValue('');
              setSecrets([]);
              setVolumes([]);
            }}
          />
        </label>
        <label>
          应用名称（查询数据卷）
          <Input
            value={app}
            disabled={busy}
            onChange={(v) => {
              setApp(v);
              setVolumes([]);
            }}
          />
        </label>
      </div>
      <Button loading={busy} onClick={() => void load()}>
        查询资源
      </Button>
      <h2>项目密钥</h2>
      {secrets.length ? (
        secrets.map((s) => <Tag key={s.name}>{s.name}</Tag>)
      ) : (
        <p className="muted">尚未查询或没有密钥。</p>
      )}
      <label className="form-field">
        内容类型
        <Select
          value={format}
          options={[
            { label: '普通密码 / 原始文件内容', value: 'raw' },
            { label: 'Redis 密码（自动生成 requirepass 配置文件）', value: 'redis' },
          ]}
          onChange={(v) => {
            setFormat(String(v));
            setName(v === 'redis' ? 'redis-config' : 'db-password');
            setValue('');
            setMultiline(false);
          }}
        />
      </label>
      <div className="form-grid">
        <label>
          新密钥名称
          <Input value={name} onChange={setName} placeholder="例如 db-password、redis-config" />
        </label>
        <label>
          密钥内容
          {multiline ? (
            <Textarea value={value} onChange={setValue} autosize={{ minRows: 3, maxRows: 8 }} />
          ) : (
            <Input type="password" value={value} onChange={setValue} autocomplete="new-password" />
          )}
        </label>
      </div>
      <Button variant="text" disabled={format === 'redis'} onClick={() => setMultiline((v) => !v)}>
        {multiline ? '切换为隐藏单行输入' : '切换为可见多行配置文件'}
      </Button>
      <Button loading={busy} disabled={!name || !value} onClick={() => void save()}>
        保存密钥
      </Button>
      <p className="muted">
        提交即清空输入，不保存到浏览器存储。连接中断后请重新输入同一内容确认；同名不同内容会被拒绝。主机
        root 仍可读取密钥。
      </p>
      <h2>应用数据卷</h2>
      {volumes.length ? (
        <ul>
          {volumes.map((v) => (
            <li key={v.name}>
              <strong>{v.name}</strong> → {v.target} · UID/GID {v.uid}:{v.gid}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">部署时创建专属数据卷。这里仅查询元数据，不提供删除数据操作。</p>
      )}
      {notice && <Alert theme="info" message={notice} />}
    </section>
  );
}
