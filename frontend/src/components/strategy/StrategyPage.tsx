import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Strategy } from '../../types/strategy';
import {
  formatCoinPoolFetchMode,
  formatCoinPoolRefreshHours,
  formatLastSignalText,
  formatSignalSourceLabel,
  formatSingleSymbolStopLoss,
} from '../../types/strategy';
import type { Account } from '../../types';
import StrategyForm from './StrategyForm';
import InlineNotice from '../ui/InlineNotice';
import ConfirmDialog from '../ui/ConfirmDialog';
import { Play, Square, AlertTriangle, Edit, Trash2, Plus, Eye } from 'lucide-react';

type ConfirmKind = { type: 'delete' | 'panic'; id: number } | null;

export default function StrategyPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<Strategy | null>(null);
  const [notice, setNotice] = useState<{ kind: 'error' | 'success' | 'info'; text: string } | null>(null);
  const [confirm, setConfirm] = useState<ConfirmKind>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [listLoading, setListLoading] = useState(false);
  const selectedAccountId = useDashboardStore((s) => s.selectedAccountId);
  const listToken = useRef(selectedAccountId);
  listToken.current = selectedAccountId;

  const load = async () => {
    const token = listToken.current;
    setListLoading(true);
    try {
      const [s, a] = await Promise.all([
        api.listStrategies(undefined, selectedAccountId ?? undefined),
        api.listAccounts(),
      ]);
      if (listToken.current !== token) return;
      setStrategies(s);
      setAccounts(a);
    } catch (e: any) {
      if (listToken.current !== token) return;
      setNotice({ kind: 'error', text: e?.message || '加载策略失败' });
    } finally {
      if (listToken.current === token) setListLoading(false);
    }
  };

  useEffect(() => {
    setStrategies([]);
    setShowForm(false);
    setEditing(null);
    load();
  }, [selectedAccountId]);

  const handleStart = async (id: number) => {
    try {
      await api.startStrategy(id);
      setNotice({ kind: 'success', text: '策略已启动' });
      load();
    } catch (e: any) {
      setNotice({ kind: 'error', text: `启动失败: ${e.message || '未知错误'}` });
    }
  };

  const handleStop = async (id: number) => {
    try {
      await api.stopStrategy(id);
      setNotice({ kind: 'success', text: '策略已停止' });
      load();
    } catch (e: any) {
      setNotice({ kind: 'error', text: `停止失败: ${e.message || '未知错误'}` });
    }
  };

  const runConfirm = async () => {
    if (!confirm) return;
    setConfirmBusy(true);
    try {
      if (confirm.type === 'panic') {
        const result = await api.panicCloseStrategy(confirm.id);
        const msgs: string[] = [];
        if (result.results?.length) {
          for (const r of result.results) {
            msgs.push(`${r.symbol} ${r.side} — ${r.status === 'ok' ? '已平仓' : '失败: ' + r.error}`);
          }
        }
        setNotice({
          kind: result.failed ? 'error' : 'success',
          text: `平仓完成: ${result.closed} 成功, ${result.failed || 0} 失败${msgs.length ? `\n${msgs.join('\n')}` : ''}`,
        });
      } else {
        await api.deleteStrategy(confirm.id);
        setNotice({ kind: 'success', text: '策略已删除' });
      }
      setConfirm(null);
      load();
    } catch (e: any) {
      setNotice({ kind: 'error', text: e.message || '操作失败' });
    } finally {
      setConfirmBusy(false);
    }
  };

  const handleSubmit = async (data: any) => {
    try {
      if (editing) {
        await api.updateStrategy(editing.id, data);
        setNotice({ kind: 'success', text: '参数已保存' });
      } else {
        await api.createStrategy(data);
        setNotice({ kind: 'success', text: '策略已创建' });
      }
      setShowForm(false);
      setEditing(null);
      load();
    } catch (e: any) {
      setNotice({
        kind: 'error',
        text: editing ? `保存失败: ${e.message || e}` : `创建失败: ${e.message || e}`,
      });
      throw e;
    }
  };

  const openCreate = () => {
    if (selectedAccountId == null) {
      setNotice({ kind: 'info', text: '请先在顶栏选择账户' });
      return;
    }
    setNotice(null);
    setEditing(null);
    setShowForm(true);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">策略管理</h2>
        <button
          onClick={openCreate}
          className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors"
        >
          <Plus size={16} />
          新建策略
        </button>
      </div>

      {notice && (
        <InlineNotice kind={notice.kind} onClose={() => setNotice(null)}>
          {notice.text}
        </InlineNotice>
      )}

      {showForm && (
        <StrategyForm
          accounts={accounts}
          defaultAccountId={editing ? editing.account_id : selectedAccountId}
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
                <button onClick={() => setConfirm({ type: 'panic', id: s.id })} className="p-1.5 text-red-400 hover:bg-red-600/20 rounded" title="紧急平仓">
                  <AlertTriangle size={16} />
                </button>
                <button onClick={() => { setEditing(s); setShowForm(true); setNotice(null); }} className="p-1.5 text-gray-400 hover:bg-gray-700 rounded" title="编辑">
                  <Edit size={16} />
                </button>
                <button onClick={() => setConfirm({ type: 'delete', id: s.id })} className="p-1.5 text-gray-400 hover:bg-red-600/20 rounded" title="删除">
                  <Trash2 size={16} />
                </button>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2 text-xs text-gray-400">
              <div>交易对: <span className="text-gray-200">{s.symbol || '选币池自动'}</span></div>
              <div>K线周期: <span className="text-gray-200">{s.timeframe}</span></div>
              {s.signal_source === 'wick_spike' ? (
                <div>接针: <span className="text-gray-200">量×{s.wick_volume_mult ?? 6} ATR×{s.wick_spike_atr_mult ?? 4} 涨跌≥{s.wick_min_move_pct ?? 3}% 回撤≤{s.wick_max_retrace_pct ?? 50}% 等量{s.wick_arm_wait_sec ?? 0}s/免撤{s.wick_arm_retrace_grace_sec ?? 5}s{(s.wick_amp_vol_relax_enabled ?? true) ? ` 放宽≥${s.wick_vol_relax_progress_start ?? 1}→${s.wick_vol_relax_progress_full ?? 1.5}@${s.wick_vol_relax_mult ?? 5}×` : ''}{(s.wick_rebound_enabled ?? true) ? ' 反弹开' : ''}{(s.wick_ema25_filter_enabled ?? true) ? ' EMA25滤' : ''} {(s.wick_atr_pct_floor_enabled ?? false) ? `低波地板${s.wick_atr_pct_floor ?? 0.5}%×${s.wick_atr_quiet_mult ?? 2}/${s.wick_atr_pct_floor2 ?? 0.25}%×${s.wick_atr_quiet_mult2 ?? 3}` : 'ATR地板关'}</span></div>
              ) : null}
              {s.signal_source === 'wavetrend' || s.signal_source === 'trend_wt' || s.signal_source === 'wick_spike' ? (
                <div>WT参数: <span className="text-gray-200">通道{s.wt_channel_length} 均线{s.wt_average_length}{s.signal_source === 'trend_wt' ? ` · ST ${s.st_timeframe_1 ?? '15m'}+${s.st_timeframe_2 ?? '30m'}` : s.signal_source === 'wick_spike' ? ' · 加仓确认用' : ''}</span></div>
              ) : s.signal_source === 'martingale_base' ? (
                <div>开仓方式: <span className="text-gray-200">每根K线开盘</span></div>
              ) : (
                <div>RSI周期: <span className="text-gray-200">{s.rsi_period}</span></div>
              )}
              <div>信号: <span className="text-gray-200">{formatSignalSourceLabel(s.signal_source, s.direction, s.rsi_entry_threshold)}</span></div>
              <div>首单仓位: <span className="text-gray-200">{s.base_qty_type === 'margin_pct' ? `保证金${s.base_qty_value}%` : `${s.base_qty_value} USDT`}{(s.skip_min_qty_exceeds ?? true) ? ' · 最小跳过' : ''}</span></div>
              <div>加仓倍数: <span className="text-gray-200">x{s.martingale_mult}</span></div>
              <div>最大层数: <span className="text-gray-200">{s.max_layers}</span></div>
              <div>跌幅触发: <span className="text-gray-200">{s.price_drop_pct}%{s.price_drop_multiplier != null && s.price_drop_multiplier !== 1 ? ` (x${s.price_drop_multiplier} 递增)` : ''}</span></div>
              <div>止盈: <span className="text-gray-200">{s.take_profit_pct}% {s.take_profit_limit_order ? '(限价单)' : '(市价单)'}</span></div>
              <div>均价止损: <span className="text-gray-200">{s.stop_loss_enabled ? `${s.stop_loss_pct}%` : '已禁用'}</span></div>
              <div>单币止损: <span className="text-gray-200">{formatSingleSymbolStopLoss(s.single_symbol_stop_loss_enabled, s.single_symbol_stop_loss_pct)}</span></div>
              <div>保证金阈值: <span className="text-gray-200">{s.margin_threshold} USDT</span></div>
              <div>选币间隔: <span className="text-gray-200">{formatCoinPoolRefreshHours(s.coin_pool_refresh_seconds)} / {formatCoinPoolFetchMode(s.coin_pool_fetch_mode, s.coin_pool_anchor_hour, s.coin_pool_anchor_minute)}</span></div>
              <div>成交量过滤: <span className="text-gray-200">
                {(s.coin_pool_min_volume_24h ?? 0) > 0
                  ? `≥ ${(s.coin_pool_min_volume_24h / 1e4).toLocaleString('zh-CN')} 万 USDT`
                  : '不限制'}
              </span></div>
              <div>TradFi过滤: <span className={s.exclude_tradefi ? 'text-amber-400' : 'text-gray-500'}>{s.exclude_tradefi ? '已排除股票永续' : '未排除'}</span></div>
              {s.use_coin_pool && (
                <div>主流币过滤: <span className={s.exclude_mainstream !== false ? 'text-sky-400' : 'text-gray-500'}>{s.exclude_mainstream !== false ? '已排除20个主流币' : '未排除'}</span></div>
              )}
              {s.use_coin_pool && s.exclude_funding && (
                <div>资金费率: <span className="text-violet-400">
                  {s.direction === 'long'
                    ? `>${s.funding_rate_threshold_pct ?? 0}% 过滤`
                    : `<${s.funding_rate_threshold_pct ?? 0}% 过滤`}
                </span></div>
              )}
              {(s.last_rsi != null || s.last_signal_at) && (
                <div className="col-span-2 mt-1 pt-1 border-t border-gray-800">
                  <span className="text-gray-500">最近信号: </span>
                  <span className={s.last_signal === 'long' ? 'text-green-400' : s.last_signal === 'short' ? 'text-red-400' : 'text-gray-400'}>
                    {formatLastSignalText(s.signal_source, s.last_signal, s.last_rsi)}
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
          <div className="col-span-2 text-center text-gray-600 py-8">
            {listLoading ? '加载中…' : '暂无策略，点击"新建策略"开始'}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirm?.type === 'panic'}
        title="确认紧急平仓？"
        detail="将以市价单平掉该策略对应账户的所有交易所持仓，此操作不可撤销。"
        confirmLabel="确认平仓"
        danger
        busy={confirmBusy}
        onConfirm={runConfirm}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm?.type === 'delete'}
        title="确定删除该策略？"
        detail="删除后无法从本页恢复。"
        confirmLabel="删除"
        danger
        busy={confirmBusy}
        onConfirm={runConfirm}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}
