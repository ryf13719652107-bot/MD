import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './index.css';

const root = document.getElementById('root');
if (root) {
  ReactDOM.createRoot(root).render(
    <BrowserRouter>
      <App />
    </BrowserRouter>
  );
} else {
  document.body.innerHTML = '<div style="color:white;padding:40px;background:#0a0a0a"><h1>错误: 找不到#root元素</h1></div>';
}
