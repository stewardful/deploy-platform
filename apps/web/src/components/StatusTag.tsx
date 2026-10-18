import { Tag } from 'tdesign-react';
const labels: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  pending: '等待部署',
  pulling: '拉取镜像',
  creating: '创建中',
  starting: '启动中',
  verifying: '启动观察',
  stopping: '停止中',
  stopped: '已停止',
  interrupted: '操作中断',
};
export const statusLabel = (value: string, application = false) =>
  application && value === 'succeeded' ? '进程已启动' : (labels[value] ?? value);
export function StatusTag({
  value,
  application = false,
}: {
  value: string;
  application?: boolean;
}) {
  const theme =
    value === 'succeeded'
      ? 'success'
      : value === 'failed' || value === 'interrupted'
        ? 'danger'
        : value === 'stopped'
          ? 'default'
          : 'primary';
  return (
    <Tag theme={theme} variant="light" shape="round">
      {statusLabel(value, application)}
    </Tag>
  );
}
