import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider } from 'tdesign-react';
import zhCN from 'tdesign-react/es/locale/zh_CN';
import 'tdesign-react/es/style/index.css';
import './styles.css';
import App from './App';
class Boundary extends React.Component<React.PropsWithChildren, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <div className="fatal">
        <h1>页面遇到问题</h1>
        <p>已接受的任务仍在节点运行。请刷新页面，重新登录后查看任务结果。</p>
        <button onClick={() => location.reload()}>重新加载</button>
      </div>
    ) : (
      this.props.children
    );
  }
}
ReactDOM.createRoot(document.getElementById('root')!).render(
  <Boundary>
    <ConfigProvider globalConfig={zhCN}>
      <App />
    </ConfigProvider>
  </Boundary>,
);
