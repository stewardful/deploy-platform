import SpecImport from './SpecImport';
import MountEditor from './MountEditor';
import { statefulTemplate } from './templates';
import { Button } from '../../components/Button';
import { useState } from 'react';
import { Alert, Input, InputNumber, Select, Textarea } from 'tdesign-react';
import { AddIcon, DeleteIcon } from 'tdesign-icons-react';
import { validateSpec, type AppSpec } from '@deploy-platform/control-client';
import { Modal } from '../../components/Modal';
const initial: AppSpec = {
  schema_version: 1,
  app: { project: 'default', name: '' },
  image: '',
  resources: { memory_mib: 128, cpu_millis: 500, pids: 64 },
  network: 'project',
  ports: [],
};
export default function DeployDialog({
  visible,
  busy,
  onClose,
  onSubmit,
}: {
  visible: boolean;
  busy: boolean;
  onClose: () => void;
  onSubmit: (spec: AppSpec) => Promise<boolean>;
}) {
  const [spec, setSpec] = useState<AppSpec>(initial);
  const [env, setEnv] = useState('{}');
  const [args, setArgs] = useState('[]');
  const [error, setError] = useState('');
  const resource = (key: keyof AppSpec['resources'], value: number) =>
    setSpec((s) => ({ ...s, resources: { ...s.resources, [key]: value } }));
  const submit = async () => {
    let environment: unknown;
    let argv: unknown;
    try {
      environment = JSON.parse(env);
      argv = JSON.parse(args);
    } catch {
      setError('高级配置必须为合法 JSON。');
      return;
    }
    if (
      !environment ||
      typeof environment !== 'object' ||
      Array.isArray(environment) ||
      Object.values(environment).some((v) => typeof v !== 'string') ||
      !Array.isArray(argv) ||
      argv.some((v) => typeof v !== 'string')
    ) {
      setError('环境变量应为字符串字典，启动参数应为字符串数组。');
      return;
    }
    const candidate: AppSpec = {
      ...spec,
      environment: environment as Record<string, string>,
      args: argv as string[],
    };
    const problem = validateSpec(candidate);
    if (problem) {
      setError(problem);
      return;
    }
    setError('');
    if (await onSubmit(candidate)) {
      setSpec(initial);
      setEnv('{}');
      setArgs('[]');
    }
  };
  return (
    <Modal
      label="部署应用"
      closeDisabled={busy}
      visible={visible}
      header="部署应用"
      width={680}
      closeOnOverlayClick={false}
      onClose={() => {
        if (!busy) onClose();
      }}
      confirmBtn={{ content: '提交部署', loading: busy }}
      cancelBtn={{ content: '取消', disabled: busy }}
      onConfirm={() => void submit()}
    >
      <p className="dialog-intro">选择已构建的镜像，为项目部署一个独立应用。</p>
      <SpecImport
        onError={setError}
        onImport={(next) => {
          setSpec(next);
          setEnv(JSON.stringify(next.environment ?? {}, null, 2));
          setArgs(JSON.stringify(next.args ?? []));
          setError('');
        }}
      />
      <div className="template-actions">
        {(['postgres', 'redis'] as const).map((kind) => (
          <Button
            key={kind}
            variant="text"
            onClick={() => {
              const next = statefulTemplate(kind, spec.app.project);
              setSpec(next);
              setEnv(JSON.stringify(next.environment ?? {}, null, 2));
              setArgs(JSON.stringify(next.args));
              setError('');
            }}
          >
            载入 {kind === 'postgres' ? 'PostgreSQL' : 'Redis'} 模板
          </Button>
        ))}
      </div>
      <div className="form-grid">
        <label>
          项目名称
          <Input
            value={spec.app.project}
            onChange={(project) => setSpec((s) => ({ ...s, app: { ...s.app, project } }))}
            placeholder="例如 my-project"
          />
        </label>
        <label>
          应用名称
          <Input
            value={spec.app.name}
            onChange={(name) => setSpec((s) => ({ ...s, app: { ...s.app, name } }))}
            placeholder="例如 api、web"
          />
        </label>
      </div>
      <label className="form-field">
        镜像引用
        <Input
          value={spec.image}
          onChange={(image) => setSpec((s) => ({ ...s, image }))}
          placeholder="registry.example.com/project/app:v1"
        />
      </label>
      <label className="form-field">
        镜像拉取策略
        <Select
          value={spec.pull_policy ?? 'always'}
          options={[
            { label: '每次拉取（默认）', value: 'always' },
            { label: '本地不存在时拉取', value: 'if_not_present' },
            { label: '仅使用已导入镜像', value: 'never' },
          ]}
          onChange={(v) => setSpec((s) => ({ ...s, pull_policy: v as AppSpec['pull_policy'] }))}
        />
      </label>
      <div className="form-grid three">
        <label>
          内存（MiB）
          <InputNumber
            value={spec.resources.memory_mib}
            min={16}
            max={4294967295}
            onChange={(v) => resource('memory_mib', Number(v))}
          />
        </label>
        <label>
          CPU（毫核）
          <InputNumber
            value={spec.resources.cpu_millis}
            min={1}
            max={4294967295}
            onChange={(v) => resource('cpu_millis', Number(v))}
          />
        </label>
        <label>
          最大进程数
          <InputNumber
            value={spec.resources.pids}
            min={1}
            max={4294967295}
            onChange={(v) => resource('pids', Number(v))}
          />
        </label>
      </div>
      <fieldset className="health-editor">
        <legend>HTTP 健康检查</legend>
        <label>
          <input
            type="checkbox"
            checked={!!spec.health_check}
            disabled={spec.network !== 'project'}
            onChange={(e) =>
              setSpec((s) => ({
                ...s,
                health_check: e.target.checked ? { port: 8080, path: '/health' } : undefined,
              }))
            }
          />
          启用 HTTP 检查（项目网络）
        </label>
        {spec.health_check && (
          <div className="form-grid">
            <label>
              检查容器端口
              <InputNumber
                min={1}
                max={65535}
                value={spec.health_check.port}
                onChange={(v) =>
                  setSpec((s) => ({ ...s, health_check: { ...s.health_check!, port: Number(v) } }))
                }
              />
            </label>
            <label>
              检查路径
              <Input
                value={spec.health_check.path}
                onChange={(path) =>
                  setSpec((s) => ({ ...s, health_check: { ...s.health_check!, path } }))
                }
              />
            </label>
          </div>
        )}
        <small>2xx 表示健康；检查不跟随跳转，也不会因为不健康反复重启应用。</small>
      </fieldset>
      <label className="form-field">
        网络模式
        <Select
          value={spec.network}
          options={[
            { label: '无网络', value: 'none' },
            { label: '项目网络（推荐，应用名互通）', value: 'project' },
            { label: '共享 Bridge（兼容旧应用）', value: 'bridge' },
          ]}
          onChange={(v) =>
            setSpec((s) => ({
              ...s,
              network: v as AppSpec['network'],
              ports: v === 'none' ? [] : s.ports,
              health_check: v === 'project' ? s.health_check : undefined,
            }))
          }
        />
      </label>
      {spec.network !== 'none' && (
        <div className="port-editor">
          <div className="field-heading">
            本机端口映射{' '}
            <Button
              variant="text"
              size="small"
              icon={<AddIcon />}
              disabled={(spec.ports?.length ?? 0) >= 16}
              onClick={() =>
                setSpec((s) => ({
                  ...s,
                  ports: [...(s.ports ?? []), { host: 18080, container: 8080 }],
                }))
              }
            >
              添加端口
            </Button>
          </div>
          {spec.ports?.map((port, i) => (
            <div className="port-row" key={i}>
              <span>127.0.0.1 :</span>
              <InputNumber
                aria-label={`主机端口 ${i + 1}`}
                value={port.host}
                min={1024}
                max={65535}
                onChange={(v) =>
                  setSpec((s) => ({
                    ...s,
                    ports: s.ports?.map((p, n) => (n === i ? { ...p, host: Number(v) } : p)),
                  }))
                }
              />
              <span>→</span>
              <InputNumber
                aria-label={`容器端口 ${i + 1}`}
                value={port.container}
                min={1}
                max={65535}
                onChange={(v) =>
                  setSpec((s) => ({
                    ...s,
                    ports: s.ports?.map((p, n) => (n === i ? { ...p, container: Number(v) } : p)),
                  }))
                }
              />
              <Button
                shape="square"
                variant="text"
                aria-label="移除端口"
                icon={<DeleteIcon />}
                onClick={() =>
                  setSpec((s) => ({ ...s, ports: s.ports?.filter((_, n) => n !== i) }))
                }
              />
            </div>
          ))}
          <small>端口仅发布到本机；外部访问需要另行配置入口。</small>
        </div>
      )}
      <MountEditor spec={spec} onChange={setSpec} />
      <p className="muted">
        数据库需完成初始化后再连接。PostgreSQL 先保存 db-password；Redis 先保存含 requirepass 的
        redis-config。模板不会自动生成业务备份。
      </p>
      <details className="advanced">
        <summary>高级配置</summary>
        <label className="form-field">
          环境变量（JSON 字符串字典）
          <Textarea value={env} onChange={setEnv} autosize={{ minRows: 2, maxRows: 6 }} />
        </label>
        <label className="form-field">
          启动参数（JSON 字符串数组）
          <Textarea value={args} onChange={setArgs} autosize={{ minRows: 2, maxRows: 6 }} />
        </label>
        <small>
          仅填写普通设置，不要填写密钥或 API
          Key。密钥请通过只读文件引用。已有应用暂不支持更换镜像、挂载或自动轮换密码。
        </small>
      </details>
      {error && <Alert theme="error" message={error} />}
      <Button
        variant="text"
        size="small"
        className="example-button"
        onClick={() => {
          setSpec({
            schema_version: 1,
            app: { project: 'demo', name: 'web' },
            image: 'docker.io/nginxinc/nginx-unprivileged:stable-alpine',
            resources: { memory_mib: 128, cpu_millis: 500, pids: 64 },
            network: 'project',
            health_check: { port: 8080, path: '/' },
            ports: [{ host: 18080, container: 8080 }],
          });
          setEnv('{}');
          setArgs('[]');
          setError('');
        }}
      >
        载入 Web 示例
      </Button>
    </Modal>
  );
}
