import { useCallback, useState } from 'react';
import { BarChart3, Play, RefreshCw, Trash2 } from 'lucide-react';
import { api, type WickStatsAnalysis, type WickStatsSummary } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';

type SignalFilter = 'wick_spike' | 'wavetrend' | 'all';

function fmtPct(s?: WickStatsSummary) {
  if (!s || !s.n) return '暂无数据';
  return `样本${s.n} 均值${s.mean?.toFixed(3)}% 中位${s.p50?.toFixed(3)}% P90=${s.p90?.toFixed(3)}%`;
}

function fmtMs(s?: WickStatsSummary) {
  if (!s || !s.n) return '暂无数据';
  return `样本${s.n} 均值${s.mean?.toFixed(0)}ms 中位${s.p50?.toFixed(0)}ms P90=${s.p90?.toFixed(0)}ms 最大${s.max?.toFixed(0)}ms`;
}

function fmtRatio(s?: WickStatsSummary) {
  if (!s || !s.n) return '暂无数据';
  return `样本${s.n} 均值${s.mean?.toFixed(3)} 中位${s.p50?.toFixed(3)} P90=${s.p90?.toFixed(3)}`;
}

function closeReasonZh(r: string | null | undefined) {
  if (!r) return '-';
  const map: Record<string, string> = {
    take_profit: '止盈',
    stop_loss: '止损',
    manual: '手动',
    panic_close: '紧急平仓',
    sync: '对账',
  };
  return map[r] || r;
}

function strategyCell(r: {
  strategy_id: number;
  strategy_name?: string;
  signal_source_zh?: string;
}) {
  return (
    <div className="leading-tight">
      <div className="font-mono text-gray-200">
        #{r.strategy_id}
        {r.strategy_name ? ` ${r.strategy_name}` : ''}
      </div>
      <div className="text-[10px] text-cyan-500/90">{r.signal_source_zh || '-'}</div>
    </div>
  );
}

export default function WickStatsPage() {
  const selectedAccountId = useDashboardStore((s) => s.selectedAccountId);
  const [signalSource, setSignalSource] = useState<SignalFilter>('wick_spike');
  const [progressMin, setProgressMin] = useState(1.5);
  const [listLimit, setListLimit] = useState(80);
  const [includeRotated, setIncludeRotated] = useState(true);
  const [enrichOpens, setEnrichOpens] = useState(true);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [data, setData] = useState<WickStatsAnalysis | null>(null);
  const [error, setError] = useState('');

  const run = useCallback(async () => {
    if (selectedAccountId == null) {
      setError('请先在顶栏选择账户');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const res = await api.analyzeWickStats({
        account_id: selectedAccountId,
        signal_source: signalSource,
        progress_min: progressMin,
        list_limit: listLimit,
        include_rotated: includeRotated,
        enrich_opens: enrichOpens,
        max_enrich: 40,
      });
      setData(res);
      if (!res.ok) setError(res.error || '分析失败');
    } catch (e: any) {
      setData(null);
      setError(e?.message || '请求失败');
    } finally {
      setLoading(false);
    }
  }, [selectedAccountId, signalSource, progressMin, listLimit, includeRotated, enrichOpens]);

  const clearLogs = useCallback(async () => {
    if (selectedAccountId == null) {
      setError('请先在顶栏选择账户');
      return;
    }
    const srcLabel =
      signalSource === 'wick_spike'
        ? '接针策略'
        : signalSource === 'wavetrend'
          ? 'WaveTrend 策略'
          : '该账户全部策略';
    if (
      !confirm(
        `确定删除顶栏账户 ID ${selectedAccountId} 下「${srcLabel}」在服务器 bot.log 中的接针日志行吗？\n` +
          '同时会清空对应策略的内存日志。\n' +
          '成交记录（trades）不会删除；如需清空请到交易历史页。\n此操作不可撤销。'
      )
    ) {
      return;
    }
    setClearing(true);
    setError('');
    try {
      const res = await api.clearWickStatsLogs({
        account_id: selectedAccountId,
        // 清空时默认按当前筛选；若只想清接针日志可选 wick_spike
        signal_source: signalSource,
        include_rotated: includeRotated,
      });
      setData(null);
      alert(res.message || `已删除 ${res.lines_removed} 行`);
    } catch (e: any) {
      setError(e?.message || '清除失败');
    } finally {
      setClearing(false);
    }
  }, [selectedAccountId, signalSource, includeRotated]);

  const deep = data?.vol_blocked_deep;
  const cf = deep?.counterfactual;
  const oq = data?.open_quality;
  const noAccount = selectedAccountId == null;

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-lg font-bold flex items-center gap-2">
            <BarChart3 size={20} className="text-cyan-400" />
            接针统计
          </h2>
          <p className="text-xs text-gray-500 mt-1">
            必须先选顶栏账户，再按信号源（接针 / WT）筛选。扫描该账户策略的服务器日志：贴尖、速度、近失卡点。
            刚上线时可先「清除接针日志」再重新采集。
          </p>
          <p className="text-xs mt-1">
            {noAccount ? (
              <span className="text-amber-400">尚未选择账户 — 请先在顶栏选择</span>
            ) : (
              <span className="text-gray-400">
                当前账户 ID：
                <span className="text-gray-200 font-mono ml-1">{selectedAccountId}</span>
                {data?.account_name ? (
                  <span className="text-gray-500 ml-2">上次分析：{data.account_name}</span>
                ) : null}
              </span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button
            type="button"
            onClick={clearLogs}
            disabled={loading || clearing || noAccount}
            title={noAccount ? '请先在顶栏选择账户' : '删除该账户相关接针日志行'}
            className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-red-900/60 text-red-300 hover:bg-red-950/40 disabled:opacity-50 text-sm"
          >
            {clearing ? <RefreshCw size={16} className="animate-spin" /> : <Trash2 size={16} />}
            {clearing ? '清除中…' : '清除接针日志'}
          </button>
          <button
            type="button"
            onClick={run}
            disabled={loading || clearing || noAccount}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-sm font-medium"
          >
            {loading ? <RefreshCw size={16} className="animate-spin" /> : <Play size={16} />}
            {loading ? '分析中…' : '运行统计'}
          </button>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 flex flex-wrap gap-4 items-end">
        <div>
          <label className="text-xs text-gray-400 block mb-1">信号源</label>
          <select
            value={signalSource}
            onChange={(e) => setSignalSource(e.target.value as SignalFilter)}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm min-w-[140px]"
          >
            <option value="wick_spike">仅接针</option>
            <option value="wavetrend">仅 WaveTrend</option>
            <option value="all">本账户全部策略</option>
          </select>
          <p className="text-[11px] text-gray-600 mt-1">统计仍只读接针日志行；WT 用于排除混入</p>
        </div>
        <div>
          <label className="text-xs text-gray-400 block mb-1">深针刺破进度 ≥</label>
          <input
            type="number"
            step={0.1}
            min={0}
            max={20}
            value={progressMin}
            onChange={(e) => setProgressMin(Number(e.target.value) || 0)}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm w-24"
          />
          <p className="text-[11px] text-gray-600 mt-1">默认 1.5（刺破后再深 50%）</p>
        </div>
        <div>
          <label className="text-xs text-gray-400 block mb-1">清单最多条数</label>
          <input
            type="number"
            min={1}
            max={500}
            value={listLimit}
            onChange={(e) => setListLimit(Number(e.target.value) || 80)}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm w-24"
          />
        </div>
        <label className="flex items-center gap-2 text-sm text-gray-300 pb-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={includeRotated}
            onChange={(e) => setIncludeRotated(e.target.checked)}
            className="rounded border-gray-600"
          />
          包含历史轮转日志
        </label>
        <label className="flex items-center gap-2 text-sm text-gray-300 pb-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={enrichOpens}
            onChange={(e) => setEnrichOpens(e.target.checked)}
            className="rounded border-gray-600"
          />
          对齐成交+最终针尖+盈亏（较慢）
        </label>
      </div>

      {error && (
        <div className="text-sm text-red-400 bg-red-950/40 border border-red-900/50 rounded-lg px-3 py-2">
          {error}
        </div>
      )}

      {data?.ok && (
        <>
          {(data.strategies?.length ?? 0) > 0 && (
            <div className="text-xs text-gray-500 flex flex-wrap gap-2">
              <span>纳入策略：</span>
              {data.strategies!.map((s) => (
                <span
                  key={s.id}
                  className="px-2 py-0.5 rounded bg-gray-800 border border-gray-700 text-gray-300"
                >
                  #{s.id} {s.name}
                  <span className="text-cyan-500 ml-1">{s.signal_source_zh}</span>
                </span>
              ))}
            </div>
          )}

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
              <div className="text-xs text-gray-500">近失记录</div>
              <div className="text-xl font-semibold text-gray-100">{data.near_miss_total}</div>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
              <div className="text-xs text-gray-500">触发 / 开仓成功</div>
              <div className="text-xl font-semibold text-gray-100">
                {data.trigger_total ?? 0} / {data.opened_total ?? 0}
              </div>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
              <div className="text-xs text-gray-500">深针被量能挡住</div>
              <div className="text-xl font-semibold text-amber-300">{deep?.count ?? 0}</div>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
              <div className="text-xs text-gray-500">若门槛改 5 倍可过</div>
              <div className="text-xl font-semibold text-emerald-300">
                {cf ? `${cf.need_5_pass}/${cf.total}` : '-'}
              </div>
            </div>
          </div>

          <div className="grid md:grid-cols-2 gap-3">
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-2">
              <h3 className="text-sm font-medium text-gray-200">进场距针尖</h3>
              <p className="text-xs text-gray-500">
                进场价相对本根极值的距离（占开盘价 %），越小越贴针尖；需新版日志字段
              </p>
              <div className="text-sm text-gray-300">开仓成功：{fmtPct(data.tip_gap_opened)}</div>
              <div className="text-sm text-gray-300">信号触发：{fmtPct(data.tip_gap_trigger)}</div>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-2">
              <h3 className="text-sm font-medium text-gray-200">接针速度</h3>
              <p className="text-xs text-gray-500">三项越低越好；成交年龄大说明推送/处理偏慢</p>
              <div className="text-sm text-gray-300">成交推送年龄：{fmtMs(data.trade_age_ms)}</div>
              <div className="text-sm text-gray-300">检出→抢锁：{fmtMs(data.detect_to_lock_ms)}</div>
              <div className="text-sm text-gray-300">
                下单：{fmtMs(data.open_api_ms ?? data.open_api_db_ms)}
              </div>
            </div>
          </div>

          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-2">
            <h3 className="text-sm font-medium text-gray-200">近失卡点分布</h3>
            {Object.keys(data.block_reasons || {}).length === 0 ? (
              <p className="text-sm text-gray-500">暂无</p>
            ) : (
              <ul className="text-sm space-y-1">
                {Object.entries(data.block_reasons).map(([k, v]) => (
                  <li key={k} className="flex justify-between gap-4 text-gray-300">
                    <span>{k}</span>
                    <span className="text-gray-100 font-mono">{v}</span>
                  </li>
                ))}
              </ul>
            )}
            {cf && cf.total > 0 && (
              <p className="text-xs text-gray-500 pt-2">
                若量能门槛改为 4.5 倍，可过 {cf.need_4_5_pass}/{cf.total}
              </p>
            )}
          </div>

          {oq && (
            <div className="space-y-3">
              <div className="bg-gray-900 border border-cyan-900/40 rounded-lg p-4 space-y-2">
                <h3 className="text-sm font-medium text-cyan-200">开仓质量：相对最终针尖 + 盈亏</h3>
                <p className="text-xs text-gray-500">
                  用本根收盘后的最高/最低作「最终针尖」，对齐 trades 看贴尖是否对应更好盈亏。
                  分析 {oq.n} 笔；匹配成交 {oq.matched_trades ?? 0}；拿到K线 {oq.kline_ok ?? 0}
                </p>
                {oq.error && <p className="text-xs text-red-400">{oq.error}</p>}
                <div className="grid md:grid-cols-3 gap-2 text-sm text-gray-300">
                  <div>距最终针尖：{fmtPct(oq.final_tip_gap)}</div>
                  <div>针尖捕获率(1=贴尖)：{fmtRatio(oq.capture_ratio)}</div>
                  <div>已平仓盈亏%：{fmtPct(oq.pnl_pct)}</div>
                </div>
                {(oq.pnl_by_tip_bucket || []).length > 0 && (
                  <div className="pt-2">
                    <div className="text-xs text-gray-500 mb-1">按距最终针尖分桶（解决与盈亏脱节）</div>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead className="text-gray-500">
                          <tr>
                            <th className="text-left py-1">分桶</th>
                            <th className="text-right py-1">样本</th>
                            <th className="text-right py-1">胜率</th>
                            <th className="text-right py-1">均盈亏%</th>
                            <th className="text-right py-1">中位盈亏%</th>
                          </tr>
                        </thead>
                        <tbody>
                          {oq.pnl_by_tip_bucket!.map((b) => (
                            <tr key={b.bucket} className="border-t border-gray-800 text-gray-300">
                              <td className="py-1">{b.bucket}</td>
                              <td className="text-right font-mono">{b.n}</td>
                              <td className="text-right font-mono">
                                {b.n ? `${((b.win_rate ?? 0) * 100).toFixed(0)}%` : '-'}
                              </td>
                              <td
                                className={`text-right font-mono ${(b.avg_pnl_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
                              >
                                {b.n ? b.avg_pnl_pct!.toFixed(3) : '-'}
                              </td>
                              <td className="text-right font-mono">
                                {b.n ? b.median_pnl_pct!.toFixed(3) : '-'}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>

              <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
                <div className="px-4 py-3 border-b border-gray-800 text-sm text-gray-200">
                  开仓明细（最终针尖 vs 盈亏）
                </div>
                <div className="overflow-x-auto max-h-[360px] overflow-y-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-gray-900 text-gray-500">
                      <tr>
                        <th className="text-left px-3 py-2">时间</th>
                        <th className="text-left px-2 py-2">策略 / 信号</th>
                        <th className="text-left px-2 py-2">币种</th>
                        <th className="text-left px-2 py-2">方向</th>
                        <th className="text-right px-2 py-2">触发贴尖%</th>
                        <th className="text-right px-2 py-2">最终贴尖%</th>
                        <th className="text-right px-2 py-2">捕获率</th>
                        <th className="text-right px-2 py-2">盈亏%</th>
                        <th className="text-left px-3 py-2">平仓原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(oq.rows || []).length === 0 ? (
                        <tr>
                          <td colSpan={9} className="px-3 py-8 text-center text-gray-600">
                            无开仓可对齐（需有 opened 日志）
                          </td>
                        </tr>
                      ) : (
                        oq.rows!.map((r, i) => (
                          <tr key={`${r.ts}-${r.symbol}-${i}`} className="border-t border-gray-800/80 text-gray-300">
                            <td className="px-3 py-1.5 whitespace-nowrap font-mono">{r.ts}</td>
                            <td className="px-2 py-1.5">{strategyCell(r)}</td>
                            <td className="px-2 py-1.5 font-mono">{r.symbol}</td>
                            <td className="px-2 py-1.5">{r.side_zh || r.side}</td>
                            <td className="px-2 py-1.5 text-right font-mono">
                              {r.tip_gap_at_trigger_pct == null ? '-' : r.tip_gap_at_trigger_pct.toFixed(3)}
                            </td>
                            <td className="px-2 py-1.5 text-right font-mono text-cyan-300">
                              {r.final_tip_gap_pct == null ? '-' : r.final_tip_gap_pct.toFixed(3)}
                            </td>
                            <td className="px-2 py-1.5 text-right font-mono">
                              {r.capture_ratio == null ? '-' : r.capture_ratio.toFixed(3)}
                            </td>
                            <td
                              className={`px-2 py-1.5 text-right font-mono ${
                                (r.pnl_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
                              }`}
                            >
                              {r.pnl_pct == null ? (r.matched ? '-' : '未匹配') : r.pnl_pct.toFixed(3)}
                            </td>
                            <td className="px-3 py-1.5">{closeReasonZh(r.close_reason)}</td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
              <h3 className="text-sm font-medium text-gray-200">
                深针量能挡住清单（刺破进度≥{deep?.progress_min ?? progressMin}）
              </h3>
              <span className="text-xs text-gray-500">
                显示 {deep?.listed ?? 0} / {deep?.count ?? 0}
              </span>
            </div>
            <div className="overflow-x-auto max-h-[420px] overflow-y-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-gray-900 text-gray-500">
                  <tr>
                    <th className="text-left px-3 py-2">时间</th>
                    <th className="text-left px-2 py-2">策略 / 信号</th>
                    <th className="text-left px-2 py-2">币种</th>
                    <th className="text-left px-2 py-2">方向</th>
                    <th className="text-right px-2 py-2">刺破进度</th>
                    <th className="text-right px-2 py-2">实际量能</th>
                    <th className="text-right px-2 py-2">量能门槛</th>
                    <th className="text-right px-2 py-2">缺口</th>
                    <th className="text-right px-3 py-2">距针尖%</th>
                  </tr>
                </thead>
                <tbody>
                  {(deep?.rows || []).length === 0 ? (
                    <tr>
                      <td colSpan={9} className="px-3 py-8 text-center text-gray-600">
                        无符合条件的记录
                      </td>
                    </tr>
                  ) : (
                    deep!.rows.map((r, i) => (
                      <tr key={`${r.ts}-${r.symbol}-${i}`} className="border-t border-gray-800/80 text-gray-300">
                        <td className="px-3 py-1.5 whitespace-nowrap font-mono">{r.ts}</td>
                        <td className="px-2 py-1.5">{strategyCell(r)}</td>
                        <td className="px-2 py-1.5 font-mono">{r.symbol}</td>
                        <td className="px-2 py-1.5">{r.direction_zh || r.direction}</td>
                        <td className="px-2 py-1.5 text-right font-mono">{r.progress.toFixed(2)}</td>
                        <td className="px-2 py-1.5 text-right font-mono text-amber-300">{r.vol_x.toFixed(2)}</td>
                        <td className="px-2 py-1.5 text-right font-mono">{r.need_x}</td>
                        <td className="px-2 py-1.5 text-right font-mono">{r.vol_shortfall.toFixed(2)}</td>
                        <td className="px-3 py-1.5 text-right font-mono">
                          {r.tip_gap_pct == null ? '-' : r.tip_gap_pct.toFixed(3)}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {data.log_files && data.log_files.length > 0 && (
            <p className="text-[11px] text-gray-600 break-all">
              已分析日志：{data.log_files.join(' · ')}
            </p>
          )}

          <details className="bg-gray-900 border border-gray-800 rounded-lg">
            <summary className="px-4 py-3 text-sm text-gray-400 cursor-pointer hover:text-gray-200">
              纯文本报告
            </summary>
            <pre className="px-4 pb-4 text-[11px] text-gray-400 whitespace-pre-wrap font-mono overflow-x-auto">
              {data.text}
            </pre>
          </details>
        </>
      )}

      {!data && !error && !loading && (
        <div className="border border-dashed border-gray-800 rounded-lg py-16 text-center text-gray-600 text-sm">
          {noAccount
            ? '请先在顶栏选择账户，再点击「运行统计」'
            : '选择信号源后点击「运行统计」分析该账户接针日志'}
        </div>
      )}
    </div>
  );
}
