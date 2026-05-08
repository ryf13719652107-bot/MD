import { useEffect, useState, useCallback, useRef } from 'react';
import { useParams, Link } from 'react-router-dom';
import { api } from '../../services/api';
import type { Strategy } from '../../types/strategy';
import type { CoinPoolEntry, Trade } from '../../types';
import { ArrowLeft, Terminal } from 'lucide-react';

interface LogEntry { time: string; level: string; message: string; }

function logColor(level: string) {
  switch (level) {
    case 'success': return 'text-green-400';
    case 'error': return 'text-red-400';
    case 'warning': return 'text-yellow-400';
    default: return 'text-gray-300';
  }
}

function fmtTime(s: string | null) {
  if (!s) return '-';
  return new Date(s).toLocaleString();
}

function pnlColor(v: number | null) {
  if (v == null) return 'text-gray-400';
  return v >= 0 ? 'text-green-400' : 'text-red-400';
}

export default function StrategyDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [pool, setPool] = useState<CoinPoolEntry[]>([]);
  const [positions, setPositions] = useState<any[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(true);

  const loadRef = useRef<() => void>(() => {});

  const load = useCallback(async () => {
    if (!id) return;
    try {
      const s = await api.getStrategy(Number(id));
      setStrategy(s);
      setLoading(false);

      if (s.use_coin_pool) {
        try {
          const source = s.coin_pool_source === 'both' ? undefined : s.coin_pool_source;
          const p = await api.getCoinPool(source);
          setPool(p);
        } catch { setPool([]); }
      }

      try {
        const ep = await api.getExchangePositions(Number(id));
        setPositions(ep);
      } catch { setPositions([]); }

      try {
        const tr = await api.listTrades({ strategy_id: Number(id), limit: 50 });
        setTrades(tr.trades);
      } catch { setTrades([]); }

      try {
        const l = await api.getStrategyLogs(Number(id), 100);
        setLogs(l);
      } catch { setLogs([]); }
    } catch {
      setStrategy(null);
      setLoading(false);
    }
  }, [id]);
  loadRef.current = load;

  useEffect(() => { load(); }, [id]);

  useEffect(() => {
    const timer = setInterval(() => loadRef.current(), 30000);
    return () => clearInterval(timer);
  }, []);

  if (loading) {
    return <div className="text-center text-gray-400 py-20">加载中...</div>;
  }

  if (!strategy) {
    return <div className="text-center text-gray-400 py-20">策略不存在</div>;
  }

  const labelClass = 'text-xs text-gray-500';
  const valClass = 'text-sm text-gray-200';

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to="/strategies" className="text-gray-400 hover:text-white transition-colors">
          <ArrowLeft size={20} />
        </Link>
        <h2 className="text-xl font-bold">{strategy.name}</h2>
        <span className={`text-xs px-2 py-0.5 rounded ${
          strategy.direction === 'long' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'
        }`}>
          {strategy.direction === 'long' ? '做多' : '做空'}
        </span>
        <span className={`text-xs px-2 py-0.5 rounded ${
          strategy.status === 'running' ? 'bg-green-600/20 text-green-400' :
          strategy.status === 'error' ? 'bg-red-600/20 text-red-400' :
          'bg-gray-700 text-gray-400'
        }`}>
          {strategy.status === 'running' ? '运行中' : strategy.status === 'error' ? '异常' : '已停止'}
        </span>
      </div>

      {/* 区块1: 策略信息 */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="font-semibold mb-3 text-sm">策略参数</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div>
            <span className={labelClass}>交易对</span>
            <div className={valClass}>{strategy.symbol || '选币池自动'}</div>
          </div>
          <div>
            <span className={labelClass}>K线周期</span>
            <div className={valClass}>{strategy.timeframe}</div>
          </div>
          <div>
            <span className={labelClass}>策略启动时间</span>
            <div className={valClass}>{fmtTime(strategy.started_at)}</div>
          </div>
          <div>
            <span className={labelClass}>信号源</span>
            <div className={valClass}>
              {strategy.signal_source === 'wavetrend' ? `WaveTrend (通道${strategy.wt_channel_length} 均线${strategy.wt_average_length})` : `RSI (周期${strategy.rsi_period} ${strategy.direction === 'long' ? '<' : '>'}${strategy.rsi_entry_threshold})`}
            </div>
          </div>
          <div>
            <span className={labelClass}>首单仓位</span>
            <div className={valClass}>{strategy.base_qty_type === 'margin_pct' ? `保证金${strategy.base_qty_value}%` : `${strategy.base_qty_value} USDT`}</div>
          </div>
          <div>
            <span className={labelClass}>加仓倍数 / 最大层数</span>
            <div className={valClass}>x{strategy.martingale_mult} / {strategy.max_layers}层</div>
          </div>
          <div>
            <span className={labelClass}>跌幅触发</span>
            <div className={valClass}>{strategy.price_drop_pct}%</div>
          </div>
          <div>
            <span className={labelClass}>止盈 / 止损</span>
            <div className={valClass}>{strategy.take_profit_pct}% ({strategy.take_profit_limit_order ? '限价单' : '市价单'}) / {strategy.stop_loss_enabled ? `${strategy.stop_loss_pct}%` : '禁用'}</div>
          </div>
          <div>
            <span className={labelClass}>杠杆 / 滑点</span>
            <div className={valClass}>{strategy.leverage}x / {strategy.slippage_pct}%</div>
          </div>
          <div>
            <span className={labelClass}>保证金阈值</span>
            <div className={valClass}>{strategy.margin_threshold} USDT</div>
          </div>
          <div>
            <span className={labelClass}>选币池</span>
            <div className={valClass}>
              {strategy.use_coin_pool
                ? `${strategy.coin_pool_source === 'both' ? '涨幅+跌幅' : strategy.coin_pool_source === 'gainers' ? '仅涨幅' : '仅跌幅'} / ${Math.round(strategy.coin_pool_refresh_seconds / 60)}分钟 / ${strategy.coin_pool_fetch_mode === 'immediate' ? '立即抓取' : '间隔抓取'}`
                : '固定交易对'}
            </div>
          </div>
          <div>
            <span className={labelClass}>TradFi / 股票永续</span>
            <div className={valClass}>
              {(strategy.exclude_tradefi ?? true) ? '已排除（TRADIFI_PERPETUAL）' : '未排除'}
            </div>
          </div>
          {strategy.last_rsi != null && (
            <div>
              <span className={labelClass}>最近信号</span>
              <div className={`text-sm ${strategy.last_signal === 'long' ? 'text-green-400' : strategy.last_signal === 'short' ? 'text-red-400' : 'text-gray-400'}`}>
                {strategy.signal_source === 'wavetrend' ? 'WT1' : 'RSI'} {strategy.last_rsi} → {strategy.last_signal === 'long' ? '做多' : strategy.last_signal === 'short' ? '做空' : strategy.last_signal}
                <span className="text-gray-600 ml-1">{strategy.last_signal_at ? new Date(strategy.last_signal_at).toLocaleTimeString() : ''}</span>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* 区块2: 当前选币池 */}
        {strategy.use_coin_pool && (
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <h3 className="font-semibold mb-3 text-sm">
              选币池
              <span className="text-gray-500 ml-2 text-xs">
                {strategy.coin_pool_source === 'both' ? '涨幅榜+跌幅榜' : strategy.coin_pool_source === 'gainers' ? '涨幅榜' : '跌幅榜'}
                ({pool.length} 个币种)
              </span>
            </h3>
            {pool.length === 0 ? (
              <div className="text-gray-600 text-sm py-4 text-center">暂无数据</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-gray-500 border-b border-gray-800">
                      <th className="text-left py-1.5 px-2">排名</th>
                      <th className="text-left py-1.5 px-2">币种</th>
                      <th className="text-right py-1.5 px-2">涨跌幅</th>
                      <th className="text-right py-1.5 px-2">来源</th>
                      <th className="text-right py-1.5 px-2">入选时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pool.map((e) => (
                      <tr key={e.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                        <td className="py-1.5 px-2 text-gray-400">#{e.rank}</td>
                        <td className="py-1.5 px-2 text-gray-200 font-mono">{e.symbol}</td>
                        <td className={`py-1.5 px-2 text-right font-mono ${e.price_change_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                          {e.price_change_pct >= 0 ? '+' : ''}{e.price_change_pct?.toFixed(2)}%
                        </td>
                        <td className="py-1.5 px-2 text-right">
                          <span className={`px-1.5 py-0.5 rounded text-xs ${e.source === 'gainers' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'}`}>
                            {e.source === 'gainers' ? '涨幅' : '跌幅'}
                          </span>
                        </td>
                        <td className="py-1.5 px-2 text-right text-gray-500">{fmtTime(e.added_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* 区块3: 当前持仓 — 交易所实时数据 */}
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="font-semibold mb-3 text-sm">
            当前持仓
            <span className="text-gray-500 ml-2 text-xs">({positions.length} 个)</span>
          </h3>
          {positions.length === 0 ? (
            <div className="text-gray-600 text-sm py-4 text-center">暂无持仓</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 border-b border-gray-800">
                    <th className="text-left py-1.5 px-2">币种</th>
                    <th className="text-left py-1.5 px-2">方向</th>
                    <th className="text-right py-1.5 px-2">USDT</th>
                    <th className="text-right py-1.5 px-2">入场价</th>
                    <th className="text-right py-1.5 px-2">未实现盈亏</th>
                    <th className="text-right py-1.5 px-2">盈亏%</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p: any, i: number) => (
                    <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                      <td className="py-1.5 px-2 text-gray-200 font-mono">{p.symbol}</td>
                      <td className="py-1.5 px-2">
                        <span className={`px-1.5 py-0.5 rounded text-xs ${p.side === 'long' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'}`}>
                          {p.side === 'long' ? '多' : '空'}
                        </span>
                      </td>
                      <td className="py-1.5 px-2 text-right text-gray-200 font-mono">{p.usdt?.toFixed(2)}</td>
                      <td className="py-1.5 px-2 text-right text-gray-200 font-mono">{p.entry_price?.toFixed(6)}</td>
                      <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(p.unrealized_pnl)}`}>
                        {p.unrealized_pnl >= 0 ? '+' : ''}{p.unrealized_pnl?.toFixed(2)}
                      </td>
                      <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(p.pnl_pct)}`}>
                        {p.pnl_pct >= 0 ? '+' : ''}{p.pnl_pct?.toFixed(2)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* 区块4: 交易记录 */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="font-semibold mb-3 text-sm">
          交易记录
          <span className="text-gray-500 ml-2 text-xs">({trades.length} 条)</span>
        </h3>
        {trades.length === 0 ? (
          <div className="text-gray-600 text-sm py-4 text-center">暂无交易记录</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left py-1.5 px-2">币种</th>
                  <th className="text-left py-1.5 px-2">方向</th>
                  <th className="text-right py-1.5 px-2">入场价</th>
                  <th className="text-right py-1.5 px-2">出场价</th>
                  <th className="text-right py-1.5 px-2">盈亏(USDT)</th>
                  <th className="text-right py-1.5 px-2">盈亏%</th>
                  <th className="text-right py-1.5 px-2">层数</th>
                  <th className="text-right py-1.5 px-2">原因</th>
                  <th className="text-right py-1.5 px-2">入场时间</th>
                  <th className="text-right py-1.5 px-2">出场时间</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                    <td className="py-1.5 px-2 text-gray-200 font-mono">{t.symbol}</td>
                    <td className="py-1.5 px-2">
                      <span className={`px-1.5 py-0.5 rounded text-xs ${t.side === 'long' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'}`}>
                        {t.side === 'long' ? '多' : '空'}
                      </span>
                    </td>
                    <td className="py-1.5 px-2 text-right text-gray-200 font-mono">{t.entry_price?.toFixed(8)}</td>
                    <td className="py-1.5 px-2 text-right text-gray-200 font-mono">{t.exit_price?.toFixed(8)}</td>
                    <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(t.realized_pnl)}`}>
                      {t.realized_pnl >= 0 ? '+' : ''}{t.realized_pnl?.toFixed(2)}
                    </td>
                    <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(t.pnl_pct)}`}>
                      {t.pnl_pct >= 0 ? '+' : ''}{t.pnl_pct?.toFixed(2)}%
                    </td>
                    <td className="py-1.5 px-2 text-right text-gray-400">L{t.layer}</td>
                    <td className="py-1.5 px-2 text-right text-gray-400">
                      {t.close_reason === 'take_profit' ? '止盈' : t.close_reason === 'stop_loss' ? '止损' : t.close_reason === 'panic_close' ? '紧急平仓' : t.close_reason === 'sync' ? '同步平仓' : t.close_reason === 'margin_stop' ? '保证金止损' : t.close_reason === 'manual' ? '手动平仓' : t.close_reason}
                    </td>
                    <td className="py-1.5 px-2 text-right text-gray-500">{fmtTime(t.entry_time)}</td>
                    <td className="py-1.5 px-2 text-right text-gray-500">{fmtTime(t.exit_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 区块5: 交易日志 */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="font-semibold mb-3 text-sm flex items-center gap-2">
          <Terminal size={14} />
          交易日志
          <span className="text-gray-500 text-xs">({logs.length} 条)</span>
        </h3>
        {logs.length === 0 ? (
          <div className="text-gray-600 text-sm py-4 text-center">暂无日志 — 策略启动后会出现执行记录</div>
        ) : (
          <div className="max-h-80 overflow-y-auto">
            <table className="w-full text-xs font-mono">
              <tbody>
                {logs.map((l, i) => (
                  <tr key={i} className="border-b border-gray-800/30">
                    <td className="py-1 pr-3 text-gray-600 whitespace-nowrap align-top w-16">{l.time}</td>
                    <td className={`py-1 pr-3 whitespace-nowrap align-top w-16 ${logColor(l.level)}`}>
                      [{l.level === 'success' ? 'OK' : l.level === 'error' ? 'ERR' : l.level === 'warning' ? 'WARN' : 'INFO'}]
                    </td>
                    <td className={`py-1 ${logColor(l.level)}`}>{l.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
