import { Input, InputNumber, Select } from 'tdesign-react';
import { Button } from '../../components/Button';
import type { AppSpec } from '@deploy-platform/control-client';

export default function MountEditor({
  spec,
  onChange,
}: {
  spec: AppSpec;
  onChange: (spec: AppSpec) => void;
}) {
  return (
    <div className="mount-editor">
      <label className="form-field">
        运行身份
        <Select
          value={spec.user ? 'numeric' : 'image'}
          options={[
            { label: '镜像默认用户', value: 'image' },
            { label: '指定 UID/GID（数据库模板使用）', value: 'numeric' },
          ]}
          onChange={(v) =>
            onChange({ ...spec, user: v === 'numeric' ? { uid: 999, gid: 999 } : undefined })
          }
        />
      </label>
      {spec.user && (
        <div className="form-grid">
          <label>
            UID
            <InputNumber
              value={spec.user.uid}
              min={0}
              max={4294967295}
              onChange={(v) => onChange({ ...spec, user: { ...spec.user!, uid: Number(v) } })}
            />
          </label>
          <label>
            GID
            <InputNumber
              value={spec.user.gid}
              min={0}
              max={4294967295}
              onChange={(v) => onChange({ ...spec, user: { ...spec.user!, gid: Number(v) } })}
            />
          </label>
        </div>
      )}
      <div className="field-heading">
        持久化数据卷
        <Button
          variant="text"
          disabled={(spec.volumes?.length ?? 0) >= 8}
          onClick={() =>
            onChange({
              ...spec,
              volumes: [...(spec.volumes ?? []), { name: 'data', target: '/data' }],
            })
          }
        >
          添加数据卷
        </Button>
      </div>
      {spec.volumes?.map((volume, index) => (
        <div className="mount-row" key={index}>
          <label>
            {`数据卷名称 ${index + 1}`}
            <Input
              value={volume.name}
              placeholder="逻辑名称"
              onChange={(name) =>
                onChange({
                  ...spec,
                  volumes: spec.volumes!.map((v, i) => (i === index ? { ...v, name } : v)),
                })
              }
            />
          </label>
          <label>
            {`数据卷路径 ${index + 1}`}
            <Input
              value={volume.target}
              placeholder="容器路径，例如 /data"
              onChange={(target) =>
                onChange({
                  ...spec,
                  volumes: spec.volumes!.map((v, i) => (i === index ? { ...v, target } : v)),
                })
              }
            />
          </label>
          <Select
            value={volume.read_only ? 'ro' : 'rw'}
            options={[
              { label: '读写', value: 'rw' },
              { label: '只读', value: 'ro' },
            ]}
            onChange={(mode) =>
              onChange({
                ...spec,
                volumes: spec.volumes!.map((v, i) =>
                  i === index ? { ...v, read_only: mode === 'ro' } : v,
                ),
              })
            }
          />
          <Button
            variant="text"
            onClick={() =>
              onChange({ ...spec, volumes: spec.volumes!.filter((_, i) => i !== index) })
            }
          >
            移除
          </Button>
        </div>
      ))}
      <small>
        由 Agent 创建应用专属目录，不接受宿主机路径；已有数据不会被清空或递归修改权限。UID/GID
        应与镜像用户匹配。
      </small>
      <label className="form-field">
        密钥引用（逗号分隔名称）
        <Input
          value={spec.secrets?.map((s) => s.name).join(',') ?? ''}
          onChange={(v) =>
            onChange({
              ...spec,
              secrets: v ? v.split(',').map((name) => ({ name: name.trim() })) : [],
            })
          }
          placeholder="例如 db-password"
        />
      </label>
      <small>
        先在“数据与密钥”保存同项目密钥。容器内只读路径为 /run/secrets/名称，配置中只保存引用。
      </small>
    </div>
  );
}
