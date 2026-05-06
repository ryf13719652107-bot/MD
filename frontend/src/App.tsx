import { Component, ReactNode } from 'react';
import { Routes, Route } from 'react-router-dom';
import AppShell from './components/layout/AppShell';
import DashboardPage from './components/dashboard/DashboardPage';
import ChartPage from './components/chart/ChartPage';
import StrategyPage from './components/strategy/StrategyPage';
import StrategyDetailPage from './components/strategy/StrategyDetailPage';
import PositionsPage from './components/positions/PositionsPage';
import TradesPage from './components/trades/TradesPage';
import CoinPoolPage from './components/coinpool/CoinPoolPage';
import SettingsPage from './components/settings/SettingsPage';

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{ color: '#fff', padding: '40px', fontFamily: 'monospace', background: '#0a0a0a', minHeight: '100vh' }}>
          <h1 style={{ color: '#ef4444', fontSize: '24px' }}>渲染错误</h1>
          <pre style={{ color: '#f87171', marginTop: '16px', whiteSpace: 'pre-wrap', fontSize: '14px' }}>
            {this.state.error.message}
          </pre>
          <pre style={{ color: '#6b7280', marginTop: '16px', whiteSpace: 'pre-wrap', fontSize: '12px', maxHeight: '400px', overflow: 'auto' }}>
            {this.state.error.stack}
          </pre>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  return (
    <ErrorBoundary>
      <AppShell>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/chart/:symbol?" element={<ChartPage />} />
          <Route path="/strategies" element={<StrategyPage />} />
          <Route path="/strategies/:id" element={<StrategyDetailPage />} />
          <Route path="/positions" element={<PositionsPage />} />
          <Route path="/trades" element={<TradesPage />} />
          <Route path="/coin-pool" element={<CoinPoolPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </AppShell>
    </ErrorBoundary>
  );
}
