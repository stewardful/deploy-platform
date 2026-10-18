import { Button } from './Button';
import { useState } from 'react';
import { Alert, Input } from 'tdesign-react';
import { ServerIcon, LockOnIcon, ArrowRightIcon } from 'tdesign-icons-react';
import { ControlClient, ControlError } from '@deploy-platform/control-client';
export function Login({ onLogin, message }: { onLogin: (token: string) => void; message: string }) {
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const login = async () => {
    setBusy(true);
    setError('');
    try {
      if (!/^[A-Za-z0-9_-]{32,128}$/.test(token.trim())) {
        setError('请输入有效的管理令牌。');
        return;
      }
      await new ControlClient(token.trim()).health();
      onLogin(token.trim());
    } catch (error) {
      setError(
        error instanceof ControlError && error.status === 401
          ? '令牌不正确，请重新输入。'
          : error instanceof Error
            ? error.message
            : '连接失败',
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="login-page">
      <section className="login-story">
        <div className="brand">
          <span className="brand-mark">
            <ServerIcon />
          </span>
          <span>
            Cloud<span className="brand-dot">.</span>
          </span>
        </div>
        <div className="login-copy">
          <div className="eyebrow">YOUR PROJECTS. ONE PLACE.</div>
          <h1>让项目有序运行。</h1>
          <p>
            部署应用，管理运行状态。
            <br />
            从一个清晰的工作台开始。
          </p>
          <div className="login-features">
            <span>独立部署</span>
            <span>轻量运行</span>
            <span>任务可追踪</span>
          </div>
        </div>
        <span className="login-footer">单节点项目管理工作台</span>
      </section>
      <main className="login-content">
        <form
          className="login-card"
          onSubmit={(event) => {
            event.preventDefault();
            void login();
          }}
        >
          <div className="login-lock">
            <LockOnIcon />
          </div>
          <h2>连接你的工作台</h2>
          <p>输入此节点的管理令牌以继续。</p>
          <label className="form-field">
            管理令牌
            <Input
              type="password"
              value={token}
              onChange={setToken}
              placeholder="输入管理令牌"
              autocomplete="off"
              aria-label="管理令牌"
              size="large"
            />
          </label>
          {message && <Alert theme="info" message={message} />}
          <div role="alert">{error && <Alert theme="error" message={error} />}</div>
          <Button type="submit" size="large" block loading={busy} icon={<ArrowRightIcon />}>
            进入工作台
          </Button>
          <small>令牌仅保留在当前页面，刷新或退出后需要重新输入。</small>
        </form>
      </main>
    </div>
  );
}
