import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Strategy } from '../../types/strategy';
import type { Account } from '../../types';
import StrategyForm from './StrategyForm';
import { Play, Square, AlertTriangle, Edit, Trash2, Plus, Eye } from 'lucide-react';

export default function StrategyPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<Strategy | null>(null);
  const selectedAccountId = useDashboardStore((s) => s.selectedAccountId);

  const load = async () => {
    const [s, a] = await Promise.all([
      api.listStrategies(undefined, selectedAccountId ?? undefined),
      api.listAccounts()
    ]);
    setStrategies(s);
    setAccounts(a);
  };

  useEffect(() => { load(); }, [selectedAccountId]);

  const handleStart = async (id: number) => {
    try {
      await api.startStrategy(id);
      load();
    } catch (e: any) {
      alert('启动失败: ' + (e.message || '未知错误'));
    }
  };

  const handleStop = async (id: number) => {
    await api.stopStrategy(id);
    load();
  };

  const handlePanicClose = async (id: number) => {
    if (!confirm('确定要紧急平仓该策略的所有持仓吗？')) return;
    await api.panicCloseStrategy(id);
    load();
  };

  const handleDelete = async (id: number) => {
    if (!confirm('确定要删除该策略吗？')) return;
    try {
      await api.deleteStrategy(id);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const handleSubmit = async (data: any) => {
    if (editing) {
      await api.updateStrategy(editing.id, data);
    } else {
      await api.createStrategy(data);
    }
    setShowForm(false);
    setEditing(null);
    load();
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">策略管理</h2>
        <button
          onClick={() => { setEditing(null); setShowForm(true); }}
          className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors"
        >
          <Plus size={16} />
          新建策略
        </button>
      </div>

      {showForm && (
        <StrategyForm
          accounts={accounts}
          initialData={editing}
          onSubmit={handleSubmit}
          onCancel={() => { setShowForm(false); setEditing(null); }}
        />
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {strategies.map((s) => (
          <div key={s.id} className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <div>
                <Link to={`/strategies/${s.id}`} className="font-semibold hover:text-blue-400 transition-colors">{s.name}</Link>
                <span className={`ml-2 text-xs px-2 py-0.5 rounded ${
                  s.direction === 'long' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'
                }`}>
                  {s.direction === 'long' ? '做多' : '做空'}
                </span>
                <span className={`ml-2 text-xs px-2 py-0.5 rounded ${
                  s.status === 'running' ? 'bg-green-600/20 text-green-400' :
                  s.status === 'error' ? 'bg-red-600/20 text-red-400' :
                  'bg-gray-700 text-gray-400'
                }`}>
                  {s.status === 'running' ? '运行中' : s.status === 'error' ? '异常' : '已停止'}
                </span>
              </div>
              <div className="flex items-center gap-1">
                <Link to={`/strategies/${s.id}`} className="p-1.5 text-blue-400 hover:bg-blue-600/20 rounded" title="查看详情">
                  <Eye size={16} />
                </Link>
                {s.status === 'stopped' || s.status === 'error' ? (
                  <button onClick={() => handleStart(s.id)} className="p-1.5 text-green-400 hover:bg-green-600/20 rounded" title="启动">
                    <Play size={16} />
                  </button>
                ) : (
                  <button onClick={() => handleStop(s.id)} className="p-1.5 text-yellow-400 hover:bg-yellow-600/20 rounded" title="停止">
                    <Square size={16} />
                  </button>
                )}
                <button onClick={() => handlePanicClose(s.id)} className="p-1.5 text-red-400 hover:bg-red-600/20 rounded" title="紧急平仓">
                  <AlertTriangle size={16} />
                </button>
                <button onClick={() => { setEditing(s); setShowForm(true); }} className="p-1.5 text-gray-400 hover:bg-gray-700 rounded" title="编辑">
                  <Edit size={16} />
                </button>
                <button onClick={() => handleDelete(s.id)} className="p-1.5 text-gray-400 hover:bg-red-600/20 rounded" title="删除">
                  <Trash2 size={16} />
                </button>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2 text-xs text-gray-400">
              <div>交易对: <span className="text-gray-200">{s.symbol || '选币池自动'}</span></div>
              <div>K线周期: <span className="text-gray-200">{s.timeframe}</span></div>
              {s.signal_source === 'wavetrend' ? (
                <div>WT参数: <span className="text-gray-200">通道{s.wt_channel_length} 均线{s.wt_average_length}</span></div>
              ) : (
                <div>RSI周期: <span className="text-gray-200">{s.rsi_period}</span></div>
              )}
              <div>信号: <span className="text-gray-200">{s.signal_source === 'wavetrend' ? 'WaveTrend' : `RSI ${s.direction === 'long' ? '<' : '>'} ${s.rsi_entry_threshold}`}</span></div>
              <div>首单仓位: <span className="text-gray-200">{s.base_qty_type === 'margin_pct' ? `保证金${s.base_qty_value}%` : `${s.base_qty_value} USDT`}</span></div>
              <div>加仓倍数: <span className="text-gray-200">x{s.martingale_mult}</span></div>
              <div>最大层数: <span className="text-gray-200">{s.max_layers}</span></div>
              <div>跌幅触发: <span className="text-gray-200">{s.price_drop_pct}%</span></div>
              <div>止盈: <span className="text-gray-200">{s.take_profit_pct}% {s.take_profit_limit_order ? '(限价单)' : '(市价单)'}</span></div>
              <div>止损: <span className="text-gray-200">{s.stop_loss_enabled ? `${s.stop_loss_pct}%` : '已禁用'}</span></div>
              <div>保证金阈值: <span className="text-gray-200">{s.margin_threshold} USDT</span></div>
              <div>选币池刷新: <span className="text-gray-200">{Math.round(s.coin_pool_refresh_seconds / 60)}分钟</span></div>
              {s.last_rsi != null && (
                <div className="col-span-2 mt-1 pt-1 border-t border-gray-800">
                  <span className="text-gray-500">最近信号: </span>
                  <span className={s.last_signal === 'long' ? 'text-green-400' : s.last_signal === 'short' ? 'text-red-400' : 'text-gray-400'}>
                    {s.signal_source === 'wavetrend' ? 'WT1' : 'RSI'} {s.last_rsi} → {s.last_signal === 'long' ? '做多' : s.last_signal === 'short' ? '做空' : '无信号'}
                  </span>
                  {s.last_signal_at && (
                    <span className="text-gray-600 ml-2">{new Date(s.last_signal_at).toLocaleTimeString()}</span>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}
        {strategies.length === 0 && (
          <div className="col-span-2 text-center text-gray-600 py-8">暂无策略，点击"新建策略"开始</div>
        )}
      </div>
    </div>
  );
}
