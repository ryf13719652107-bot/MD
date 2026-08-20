import { useEffect } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import {
  COIN_POOL_REFRESH_OPTIONS,
  formatAnchorTime,
  nearestCoinPoolRefreshSeconds,
  parseAnchorTime,
  type Strategy,
  type StrategyApiPayload,
  type StrategyFormData,
} from '../../types/strategy';
import type { Account } from '../../types';

const schema = z.object({
  account_id: z.number().min(1, '请选择账户'),
  name: z.string().min(1, '请输入策略名称').max(100),
  direction: z.enum(['long', 'short']),
  symbol: z.string().optional().or(z.literal('')),
  signal_source: z.enum(['rsi', 'wavetrend', 'trend_wt', 'martingale_base', 'wick_spike']),
  rsi_period: z.number().min(5).max(50),
  timeframe: z.enum(['1m', '5m', '15m', '1h']),
  wt_channel_length: z.number().min(2).max(50),
  wt_average_length: z.number().min(2).max(100),
  wt_ob_level: z.number().min(10).max(100),
  wt_os_level: z.number().min(-100).max(-10),
  st_atr_period: z.number().min(1).max(100),
  st_factor: z.number().min(0.1).max(20),
  st_timeframe_1: z.enum(['5m', '15m', '30m', '1h', '4h']),
  st_timeframe_2: z.enum(['5m', '15m', '30m', '1h', '4h']),
  wick_volume_mult: z.number().min(0).max(100),
  wick_volume_sma_period: z.number().min(2).max(200),
  wick_atr_period: z.number().min(2).max(100),
  wick_spike_atr_mult: z.number().min(0.1).max(50),
  wick_cooldown_sec: z.number().min(0).max(3600),
  wick_amp_vol_relax_enabled: z.boolean(),
  wick_vol_relax_progress_start: z.number().min(0).max(10),
  wick_vol_relax_progress_full: z.number().min(0).max(10),
  wick_vol_relax_mult: z.number().min(0).max(100),
  wick_min_move_pct: z.number().min(0).max(50),
  wick_max_retrace_pct: z.number().min(0).max(100),
  wick_arm_wait_sec: z.number().min(0).max(120),
  wick_arm_retrace_grace_sec: z.number().min(0).max(60),
  wick_arm_grace_max_tip_gap_pct: z.number().min(0).max(20),
  wick_rebound_enabled: z.boolean(),
  wick_ema25_filter_enabled: z.boolean(),
  wick_rebound_trigger_pct: z.number().min(0).max(100),
  wick_rebound_abort_pct: z.number().min(0).max(100),
  wick_rebound_wait_sec: z.number().min(0).max(60),
  wick_martingale_mode: z.enum(['price_drop', 'price_and_wt']),
  trailing_tp_enabled: z.boolean(),
  trailing_tp_window_sec: z.number().min(1).max(3600),
  trailing_tp_drawdown_base_pct: z.number().min(0).max(100),
  trailing_tp_drawdown_tier1_pct: z.number().min(0).max(100),
  trailing_tp_drawdown_tier2_pct: z.number().min(0).max(100),
  trailing_tp_tier1_threshold: z.number().min(0).max(100),
  trailing_tp_tier2_threshold: z.number().min(0).max(100),
  margin_threshold: z.number().min(0),
  base_qty_type: z.enum(['margin_pct', 'usdt']),
  base_qty_value: z.number().min(0.01),
  skip_min_qty_exceeds: z.coerce.boolean(),
  rsi_entry_threshold: z.number().min(0).max(100),
  price_drop_pct: z.number().min(0.1).max(100),
  price_drop_multiplier: z.number().min(1).max(5),
  martingale_mult: z.number().min(1).max(10),
  max_layers: z.coerce
    .number({ invalid_type_error: '请输入数字' })
    .int({ message: '最大加仓次数须为整数' })
    .min(1, '至少为 1')
    .max(200, '最大为 200'),
  martingale_rsi_enabled: z.coerce.boolean(),
  martingale_st_filter_enabled: z.coerce.boolean(),
  take_profit_pct: z.number().min(0.1).max(50),
  take_profit_limit_order: z.coerce.boolean(),
  stop_loss_enabled: z.coerce.boolean(),
  stop_loss_pct: z.number().min(0.1).max(100),
  single_symbol_stop_loss_enabled: z.coerce.boolean(),
  single_symbol_stop_loss_pct: z.number().min(0.1).max(100),
  leverage: z.number().min(1).max(125),
  use_coin_pool: z.coerce.boolean(),
  coin_pool_source: z.enum(['gainers', 'losers', 'both']),
  coin_pool_refresh_seconds: z.number().refine(
    (v) => [600, 900, 1800, 3600, 7200, 14400, 28800, 43200, 86400].includes(v),
    '请选择有效的选币间隔',
  ),
  coin_pool_fetch_mode: z.enum(['immediate', 'interval', 'scheduled']),
  coin_pool_anchor_time: z.string().regex(/^\d{2}:\d{2}$/, '请输入 HH:mm 格式时间'),
  coin_pool_top_n: z.number().min(1).max(50),
  /** 表单内以「万 USDT」录入，提交时 ×1e4 转为 USDT */
  coin_pool_min_volume_24h: z.number().min(0).max(99999999),
  exclude_tradefi: z.coerce.boolean(),
  exclude_delisting: z.coerce.boolean(),
  exclude_mainstream: z.coerce.boolean(),
  exclude_funding: z.coerce.boolean(),
  funding_rate_threshold_pct: z.number().min(-5).max(5),
}).superRefine((data, ctx) => {
  if (!data.wick_rebound_enabled) return;
  if (data.wick_rebound_trigger_pct <= 0) return; // 0=confirm后立刻市价
  if (
    data.wick_rebound_abort_pct > 0
    && data.wick_rebound_abort_pct <= data.wick_rebound_trigger_pct
  ) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['wick_rebound_abort_pct'],
      message: '反弹放弃% 必须大于 反弹触发%（或填 0 关闭放弃）',
    });
  }
});

const WAN = 1e4;

interface Props {
  accounts: Account[];
  /** 新建时默认账户（顶栏当前账户）；编辑时忽略 */
  defaultAccountId?: number | null;
  initialData: Strategy | null;
  onSubmit: (data: StrategyApiPayload) => void;
  onCancel: () => void;
}

function toFormDefaults(
  initialData: Strategy | null,
  accounts: Account[],
  defaultAccountId?: number | null,
): StrategyFormData {
  if (initialData) {
    return {
      account_id: initialData.account_id,
      name: initialData.name,
      direction: initialData.direction,
      symbol: initialData.symbol || '',
      signal_source: initialData.signal_source ?? 'rsi',
      rsi_period: initialData.rsi_period,
      timeframe: initialData.timeframe as '1m' | '5m' | '15m' | '1h',
      wt_channel_length: initialData.wt_channel_length ?? 10,
      wt_average_length: initialData.wt_average_length ?? 21,
      wt_ob_level: initialData.wt_ob_level ?? 60,
      wt_os_level: initialData.wt_os_level ?? -60,
      st_atr_period: initialData.st_atr_period ?? 10,
      st_factor: initialData.st_factor ?? 3,
      st_timeframe_1: (initialData.st_timeframe_1 as StrategyFormData['st_timeframe_1']) || '15m',
      st_timeframe_2: (initialData.st_timeframe_2 as StrategyFormData['st_timeframe_2']) || '30m',
      wick_volume_mult: initialData.wick_volume_mult ?? 6,
      wick_volume_sma_period: initialData.wick_volume_sma_period ?? 20,
      wick_atr_period: initialData.wick_atr_period ?? 14,
      wick_spike_atr_mult: initialData.wick_spike_atr_mult ?? 4,
      wick_cooldown_sec: initialData.wick_cooldown_sec ?? 0,
      wick_amp_vol_relax_enabled: initialData.wick_amp_vol_relax_enabled ?? true,
      wick_vol_relax_progress_start: initialData.wick_vol_relax_progress_start ?? 1,
      wick_vol_relax_progress_full: initialData.wick_vol_relax_progress_full ?? 1.5,
      wick_vol_relax_mult: initialData.wick_vol_relax_mult ?? 5,
      wick_min_move_pct: initialData.wick_min_move_pct ?? 3,
      wick_max_retrace_pct: initialData.wick_max_retrace_pct ?? 50,
      wick_arm_wait_sec: initialData.wick_arm_wait_sec ?? 12,
      wick_arm_retrace_grace_sec: initialData.wick_arm_retrace_grace_sec ?? 5,
      wick_arm_grace_max_tip_gap_pct: initialData.wick_arm_grace_max_tip_gap_pct ?? 2,
      wick_rebound_enabled: initialData.wick_rebound_enabled ?? true,
      wick_ema25_filter_enabled: initialData.wick_ema25_filter_enabled ?? true,
      wick_rebound_trigger_pct: initialData.wick_rebound_trigger_pct ?? 20,
      wick_rebound_abort_pct: initialData.wick_rebound_abort_pct ?? 35,
      wick_rebound_wait_sec: initialData.wick_rebound_wait_sec ?? 0,
      wick_martingale_mode:
        initialData.wick_martingale_mode === 'price_drop' ? 'price_drop' : 'price_and_wt',
      trailing_tp_enabled: initialData.trailing_tp_enabled ?? false,
      trailing_tp_window_sec: initialData.trailing_tp_window_sec ?? 300,
      trailing_tp_drawdown_base_pct: initialData.trailing_tp_drawdown_base_pct ?? 30,
      trailing_tp_drawdown_tier1_pct: initialData.trailing_tp_drawdown_tier1_pct ?? 20,
      trailing_tp_drawdown_tier2_pct: initialData.trailing_tp_drawdown_tier2_pct ?? 15,
      trailing_tp_tier1_threshold: initialData.trailing_tp_tier1_threshold ?? 2.5,
      trailing_tp_tier2_threshold: initialData.trailing_tp_tier2_threshold ?? 5.0,
      margin_threshold: initialData.margin_threshold,
      base_qty_type: initialData.base_qty_type,
      base_qty_value: initialData.base_qty_value,
      skip_min_qty_exceeds: initialData.skip_min_qty_exceeds ?? true,
      rsi_entry_threshold: initialData.rsi_entry_threshold,
      price_drop_pct: initialData.price_drop_pct,
      price_drop_multiplier: initialData.price_drop_multiplier ?? 1,
      martingale_mult: initialData.martingale_mult,
      max_layers: Number(initialData.max_layers ?? 8),
      martingale_rsi_enabled: initialData.martingale_rsi_enabled ?? true,
      martingale_st_filter_enabled: initialData.martingale_st_filter_enabled ?? false,
      take_profit_pct: initialData.take_profit_pct,
      take_profit_limit_order: initialData.take_profit_limit_order,
      stop_loss_enabled: initialData.stop_loss_enabled ?? true,
      stop_loss_pct: initialData.stop_loss_pct,
      single_symbol_stop_loss_enabled: initialData.single_symbol_stop_loss_enabled ?? false,
      single_symbol_stop_loss_pct: initialData.single_symbol_stop_loss_pct ?? 10,
      slippage_pct: initialData.slippage_pct ?? 0.5,
      leverage: initialData.leverage ?? 10,
      use_coin_pool: initialData.use_coin_pool,
      coin_pool_source: initialData.coin_pool_source,
      coin_pool_refresh_seconds: nearestCoinPoolRefreshSeconds(initialData.coin_pool_refresh_seconds ?? 3600),
      coin_pool_fetch_mode:
        initialData.coin_pool_fetch_mode === 'scheduled' || initialData.coin_pool_fetch_mode === 'immediate'
          ? initialData.coin_pool_fetch_mode
          : 'interval',
      coin_pool_anchor_time: formatAnchorTime(
        initialData.coin_pool_anchor_hour,
        initialData.coin_pool_anchor_minute,
      ),
      coin_pool_top_n: initialData.coin_pool_top_n ?? 20,
      coin_pool_min_volume_24h: (initialData.coin_pool_min_volume_24h ?? 0) / WAN,
      exclude_tradefi: initialData.exclude_tradefi ?? true,
      exclude_delisting: initialData.exclude_delisting ?? true,
      exclude_mainstream: initialData.exclude_mainstream ?? true,
      exclude_funding: initialData.exclude_funding ?? false,
      funding_rate_threshold_pct: initialData.funding_rate_threshold_pct ?? 0,
    };
  }
  const preferred =
    defaultAccountId != null && accounts.some((a) => a.id === defaultAccountId)
      ? defaultAccountId
      : accounts[0]?.id || 0;
  return {
    account_id: preferred,
    name: '',
    direction: 'long',
    symbol: '',
    signal_source: 'wavetrend',
    rsi_period: 14,
    timeframe: '1m',
    wt_channel_length: 10,
    wt_average_length: 21,
    wt_ob_level: 60,
    wt_os_level: -60,
    st_atr_period: 10,
    st_factor: 3,
    st_timeframe_1: '15m',
    st_timeframe_2: '30m',
    wick_volume_mult: 6,
    wick_volume_sma_period: 20,
    wick_atr_period: 14,
    wick_spike_atr_mult: 4,
    wick_cooldown_sec: 0,
    wick_amp_vol_relax_enabled: true,
    wick_vol_relax_progress_start: 1,
    wick_vol_relax_progress_full: 1.5,
    wick_vol_relax_mult: 5,
    wick_min_move_pct: 3,
    wick_max_retrace_pct: 50,
    wick_arm_wait_sec: 12,
    wick_arm_retrace_grace_sec: 5,
    wick_arm_grace_max_tip_gap_pct: 2,
    wick_rebound_enabled: true,
    wick_ema25_filter_enabled: true,
    wick_rebound_trigger_pct: 20,
    wick_rebound_abort_pct: 35,
    wick_rebound_wait_sec: 0,
    wick_martingale_mode: 'price_and_wt',
    trailing_tp_enabled: false,
    trailing_tp_window_sec: 300,
    trailing_tp_drawdown_base_pct: 30,
    trailing_tp_drawdown_tier1_pct: 20,
    trailing_tp_drawdown_tier2_pct: 15,
    trailing_tp_tier1_threshold: 2.5,
    trailing_tp_tier2_threshold: 5.0,
    margin_threshold: 0,
    base_qty_type: 'margin_pct',
    base_qty_value: 6,
    skip_min_qty_exceeds: true,
    rsi_entry_threshold: 30,
    price_drop_pct: 30,
    price_drop_multiplier: 1,
    martingale_mult: 1.5,
    max_layers: 8,
    martingale_rsi_enabled: true,
    martingale_st_filter_enabled: false,
    take_profit_pct: 2,
    take_profit_limit_order: true,
    stop_loss_enabled: false,
    stop_loss_pct: 5,
    single_symbol_stop_loss_enabled: false,
    single_symbol_stop_loss_pct: 10,
    slippage_pct: 0.5,
    leverage: 10,
    use_coin_pool: true,
    coin_pool_source: 'gainers',
    coin_pool_refresh_seconds: 3600,
    coin_pool_fetch_mode: 'interval',
    coin_pool_anchor_time: '08:00',
    coin_pool_top_n: 20,
    coin_pool_min_volume_24h: 0,
    exclude_tradefi: true,
    exclude_delisting: true,
    exclude_mainstream: true,
    exclude_funding: false,
    funding_rate_threshold_pct: 0,
  };
}

function toApiPayload(data: StrategyFormData): StrategyApiPayload {
  const { coin_pool_anchor_time, ...rest } = data;
  const { hour, minute } = parseAnchorTime(coin_pool_anchor_time);
  return {
    ...rest,
    coin_pool_refresh_seconds: nearestCoinPoolRefreshSeconds(data.coin_pool_refresh_seconds),
    coin_pool_anchor_hour: hour,
    coin_pool_anchor_minute: minute,
    coin_pool_min_volume_24h: (data.coin_pool_min_volume_24h || 0) * WAN,
  };
}

export default function StrategyForm({
  accounts,
  defaultAccountId = null,
  initialData,
  onSubmit,
  onCancel,
}: Props) {
  // 新建：只展示顶栏当前账户；编辑：账户字段本身不展示
  const accountOptions =
    !initialData && defaultAccountId != null
      ? accounts.filter((a) => a.id === defaultAccountId)
      : accounts;

  const {
    register, handleSubmit, watch, setValue, getValues, formState: { errors },
  } = useForm<StrategyFormData>({
    resolver: zodResolver(schema),
    defaultValues: toFormDefaults(initialData, accountOptions, defaultAccountId),
  });

  const direction = watch('direction', 'long');
  const signalSource = watch('signal_source', 'rsi');
  const useCoinPool = watch('use_coin_pool', true);
  const coinPoolSource = watch('coin_pool_source', 'gainers');
  const fetchMode = watch('coin_pool_fetch_mode', 'interval');
  const stopLossEnabled = watch('stop_loss_enabled', true);
  const singleSymbolStopLossEnabled = watch('single_symbol_stop_loss_enabled', false);
  const excludeFunding = watch('exclude_funding', false);
  const martingaleRsiEnabled = watch('martingale_rsi_enabled', true);
  const wickMartingaleMode = watch('wick_martingale_mode', 'price_and_wt');
  const trailingTpEnabled = watch('trailing_tp_enabled', false);

  // Auto-adjust RSI threshold on mount and when direction changes
  useEffect(() => {
    const cur = getValues('rsi_entry_threshold');
    if (direction === 'short' && cur < 50) {
      setValue('rsi_entry_threshold', 70);
    } else if (direction === 'long' && cur > 50) {
      setValue('rsi_entry_threshold', 30);
    }
  }, [direction]);

  const inputClass = 'w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-white focus:border-blue-500 focus:outline-none';
  const labelClass = 'block text-xs text-gray-400 mb-0.5';
  const errorClass = 'text-red-400 text-xs';

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="font-semibold mb-4">{initialData ? '编辑策略' : '新建策略'}</h3>
      <form onSubmit={handleSubmit((data) => onSubmit(toApiPayload(data)))} className="space-y-3">
        <div className="grid grid-cols-3 gap-3">
          {!initialData && (
            <div>
              <label className={labelClass}>交易账户</label>
              <select {...register('account_id', { valueAsNumber: true })} className={inputClass}>
                {accountOptions.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} [{a.exchange === 'gate' ? 'GATE' : '币安'}] {a.testnet ? '(测试网)' : '(实盘)'}
                  </option>
                ))}
              </select>
              {accountOptions.length === 0 && (
                <p className={errorClass}>请先在顶栏选择账户</p>
              )}
              {errors.account_id && <p className={errorClass}>{errors.account_id.message}</p>}
            </div>
          )}
          <div>
            <label className={labelClass}>策略名称</label>
            <input {...register('name')} className={inputClass} placeholder="输入策略名称" />
            {errors.name && <p className={errorClass}>{errors.name.message}</p>}
          </div>
          <div>
            <label className={labelClass}>交易方向</label>
            <select {...register('direction')} className={inputClass}>
              <option value="long">做多</option>
              <option value="short">做空</option>
            </select>
          </div>
        </div>

        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>信号源</label>
            <select {...register('signal_source')} className={inputClass}>
              <option value="rsi">RSI</option>
              <option value="wavetrend">WaveTrend</option>
              <option value="trend_wt">趋势WT（WT + 超级趋势过滤）</option>
              <option value="martingale_base">基础马丁（每根K线开盘开首单）</option>
              <option value="wick_spike">毫秒接针（仅币安）</option>
            </select>
          </div>
          <div>
            <label className={labelClass}>K线周期</label>
            <select {...register('timeframe')} className={inputClass}>
              <option value="1m">1分钟</option>
              <option value="5m">5分钟</option>
              <option value="15m">15分钟</option>
              <option value="1h">1小时</option>
            </select>
            <span className="text-xs text-gray-600">
              {signalSource === 'wick_spike'
                ? '接针用此周期算 ATR/成交量；开仓由成交价流触发'
                : '按K线收盘后执行'}
            </span>
          </div>
        </div>

        {signalSource === 'rsi' && (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={labelClass}>RSI 周期</label>
              <input type="number" {...register('rsi_period', { valueAsNumber: true })} className={inputClass} />
            </div>
            <div>
              <label className={labelClass}>RSI 入场阈值</label>
              <input type="number" step="0.1" {...register('rsi_entry_threshold', { valueAsNumber: true })} className={inputClass} />
              <span className="text-xs text-gray-600">{direction === 'long' ? 'RSI低于阈值时开多' : 'RSI高于阈值时开空'}</span>
            </div>
          </div>
        )}

        {signalSource === 'wick_spike' && (
          <div className="space-y-3">
            <div className="rounded-md border border-cyan-700/50 bg-cyan-900/20 px-3 py-2 text-xs text-cyan-200 space-y-1">
              <p>毫秒接针：仅币安。先放量（当前量 ≥ Vol SMA × 倍数），再用本根极值追认「开盘价 ± 上根 ATR × 倍数」。默认开启「市价反弹追踪」：confirm 后等针尖反弹再开仓。</p>
              <p>调度错峰：持仓管理在每根 K 第 40 秒，止盈检测第 30 秒；:00 附近不占锁，留给价流开仓。止盈/层数下方手填；加仓模式在马丁区选择「仅涨跌幅」或「涨跌幅+WT」。</p>
            </div>
            <div>
              <label className={`${labelClass} flex items-center gap-2`}>
                <span>progress 量能放宽</span>
                <label className="relative inline-flex items-center cursor-pointer">
                  <input type="checkbox" {...register('wick_amp_vol_relax_enabled')} className="sr-only peer" />
                  <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
                </label>
              </label>
              <span className="text-xs text-gray-600">
                默认开：progress = |极值-开盘|/N；刺破后按进度把放量倍数线性降到下方「放宽后量能」
              </span>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={labelClass}>放量倍数（相对 Vol SMA）</label>
                <input type="number" step="0.1" {...register('wick_volume_mult', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">默认 6；填 0 关闭放量过滤</span>
              </div>
              <div>
                <label className={labelClass}>成交量 SMA 周期</label>
                <input type="number" {...register('wick_volume_sma_period', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">默认 20</span>
              </div>
              <div>
                <label className={labelClass}>ATR 周期</label>
                <input type="number" {...register('wick_atr_period', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">用上根已收盘 ATR，默认 14</span>
              </div>
              <div>
                <label className={labelClass}>刺出 ATR 倍数</label>
                <input type="number" step="0.1" {...register('wick_spike_atr_mult', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">N = ATR × 该值；默认 4</span>
              </div>
              <div>
                <label className={labelClass}>最小涨跌幅 %</label>
                <input type="number" step="0.1" {...register('wick_min_move_pct', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">相对本根开盘；默认 3，填 0 关闭</span>
              </div>
              <div>
                <label className={labelClass}>最大回撤 %</label>
                <input type="number" step="1" {...register('wick_max_retrace_pct', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">相对开盘→极值；默认 50（收回一半跳过）；填 0 关闭</span>
              </div>
              <div>
                <label className={labelClass}>刺破等量窗口(秒)</label>
                <input type="number" step="1" {...register('wick_arm_wait_sec', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">刺破后最多等多久量能；默认 12，填 0 关闭武装</span>
              </div>
              <div>
                <label className={labelClass}>等量免回撤(秒)</label>
                <input type="number" step="0.5" {...register('wick_arm_retrace_grace_sec', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">武装时量不够，确认前 N 秒免回撤；默认 5</span>
              </div>
              <div>
                <label className={labelClass}>免回撤 tip_gap% 上限</label>
                <input type="number" step="0.1" {...register('wick_arm_grace_max_tip_gap_pct', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">grace 生效时离针尖过远不开；默认 2，填 0 不限制</span>
              </div>
              <div>
                <label className={labelClass}>开始放宽 progress</label>
                <input type="number" step="0.1" {...register('wick_vol_relax_progress_start', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">默认 1.0（刚刺破）</span>
              </div>
              <div>
                <label className={labelClass}>完全放宽 progress</label>
                <input type="number" step="0.1" {...register('wick_vol_relax_progress_full', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">默认 1.5</span>
              </div>
              <div>
                <label className={labelClass}>放宽后量能倍数</label>
                <input type="number" step="0.1" {...register('wick_vol_relax_mult', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">默认 5；不会高于上方放量倍数</span>
              </div>
              <div>
                <label className={labelClass}>同币额外冷却（秒）</label>
                <input type="number" {...register('wick_cooldown_sec', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">默认 0（仅同币同根 K 去重）</span>
              </div>
            </div>
            <div>
              <label className={`${labelClass} flex items-center gap-2`}>
                <span>市价反弹追踪（方案J）</span>
                <label className="relative inline-flex items-center cursor-pointer">
                  <input type="checkbox" {...register('wick_rebound_enabled')} className="sr-only peer" />
                  <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
                </label>
              </label>
              <span className="text-xs text-gray-600">
                默认开。confirm 达标不立刻下单，等价格从针尖反弹到「触发%」再市价；针尖可加深。confirm 回撤上限会与「放弃%」取较小，避免进窗即放弃。
              </span>
            </div>
            <div>
              <label className={`${labelClass} flex items-center gap-2`}>
                <span>1m 开盘 vs EMA25 过滤</span>
                <label className="relative inline-flex items-center cursor-pointer">
                  <input type="checkbox" {...register('wick_ema25_filter_enabled')} className="sr-only peer" />
                  <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
                </label>
              </label>
              <span className="text-xs text-gray-600">
                默认开。做空：1m 开盘价低于 EMA25 不做空；做多：1m 开盘价高于 EMA25 不做多。用已收盘 K 内存计算，不影响下单速度。
              </span>
            </div>
            <div className="grid grid-cols-3 gap-3">
              <div>
                <label className={labelClass}>反弹触发 %</label>
                <input type="number" step="1" {...register('wick_rebound_trigger_pct', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">占针深；默认 20；填 0=confirm 后立刻市价</span>
              </div>
              <div>
                <label className={labelClass}>反弹放弃 %</label>
                <input type="number" step="1" {...register('wick_rebound_abort_pct', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">须大于触发%；默认 35；0=关闭放弃（仅超时）</span>
              </div>
              <div>
                <label className={labelClass}>等反弹超时(秒)</label>
                <input type="number" step="0.5" {...register('wick_rebound_wait_sec', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">针尖停住后再等反弹最久秒数（破新尖会重置计时）；默认 0=本根内不超时，换根未反弹仍超时</span>
              </div>
            </div>
          </div>
        )}

        {(signalSource === 'wavetrend' || signalSource === 'trend_wt') && (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={labelClass}>WT 通道长度</label>
              <input type="number" {...register('wt_channel_length', { valueAsNumber: true })} className={inputClass} />
              <span className="text-xs text-gray-600">WT1周期，默认10</span>
            </div>
            <div>
              <label className={labelClass}>WT 均线长度</label>
              <input type="number" {...register('wt_average_length', { valueAsNumber: true })} className={inputClass} />
              <span className="text-xs text-gray-600">WT2平滑周期，默认21</span>
            </div>
            <div>
              <label className={labelClass}>WT 超买线</label>
              <input type="number" step="1" {...register('wt_ob_level', { valueAsNumber: true })} className={inputClass} />
              <span className="text-xs text-gray-600">死叉+WT1高于此值开空，默认60</span>
            </div>
            <div>
              <label className={labelClass}>WT 超卖线</label>
              <input type="number" step="1" {...register('wt_os_level', { valueAsNumber: true })} className={inputClass} />
              <span className="text-xs text-gray-600">金叉+WT1低于此值开多，默认-60</span>
            </div>
          </div>
        )}

        {signalSource === 'trend_wt' && (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={labelClass}>超级趋势 ATR 周期</label>
              <input type="number" {...register('st_atr_period', { valueAsNumber: true })} className={inputClass} />
              <span className="text-xs text-gray-600">默认10</span>
            </div>
            <div>
              <label className={labelClass}>超级趋势 Factor</label>
              <input type="number" step="0.1" {...register('st_factor', { valueAsNumber: true })} className={inputClass} />
              <span className="text-xs text-gray-600">默认3.0</span>
            </div>
            <div>
              <label className={labelClass}>超级趋势周期1</label>
              <select {...register('st_timeframe_1')} className={inputClass}>
                <option value="5m">5分钟</option>
                <option value="15m">15分钟</option>
                <option value="30m">30分钟</option>
                <option value="1h">1小时</option>
                <option value="4h">4小时</option>
              </select>
            </div>
            <div>
              <label className={labelClass}>超级趋势周期2</label>
              <select {...register('st_timeframe_2')} className={inputClass}>
                <option value="5m">5分钟</option>
                <option value="15m">15分钟</option>
                <option value="30m">30分钟</option>
                <option value="1h">1小时</option>
                <option value="4h">4小时</option>
              </select>
              <span className="text-xs text-gray-600">两周期同向才开仓（默认15m+30m）</span>
            </div>
          </div>
        )}

        {signalSource === 'martingale_base' && (
          <div className="rounded-md border border-amber-700/50 bg-amber-900/20 px-3 py-2 text-xs text-amber-300 space-y-1">
            <p>
              基础马丁：不计算任何指标，每根K线开盘时对无持仓的币按「{direction === 'long' ? '做多' : '做空'}」方向直接开首单，之后由马丁加仓/止盈逻辑接管。
            </p>
            <p>
              {useCoinPool
                ? '可与选币池配合：对池内每个币独立开首单，TradFi/下架/主流/资金费率等过滤与 RSI、WaveTrend 相同。'
                : '当前为固定交易对模式；开启选币池后将对池内每个币独立开首单。'}
            </p>
          </div>
        )}

        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>首单仓位类型</label>
            <select {...register('base_qty_type')} className={inputClass}>
              <option value="margin_pct">保证金百分比</option>
              <option value="usdt">固定USDT金额</option>
            </select>
          </div>
          <div>
            <label className={labelClass}>首单仓位数值</label>
            <input type="number" step="0.01" {...register('base_qty_value', { valueAsNumber: true })} className={inputClass} />
          </div>
          <div>
            <label className={`${labelClass} flex items-center gap-2`}>
              <span>选币方式</span>
              <label className="relative inline-flex items-center cursor-pointer">
                <input type="checkbox" {...register('use_coin_pool')} className="sr-only peer" />
                <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
              </label>
              <span className="text-xs text-gray-500">{watch('use_coin_pool') ? '选币池自动' : '固定交易对'}</span>
            </label>
          </div>
        </div>

        <div className="rounded-lg border border-sky-500/35 bg-sky-950/20 px-3 py-2.5 flex items-start gap-3">
          <label className="relative inline-flex items-center cursor-pointer mt-0.5 shrink-0">
            <input type="checkbox" {...register('skip_min_qty_exceeds')} className="sr-only peer" />
            <div className="w-9 h-5 bg-gray-600 peer-checked:bg-sky-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all" />
          </label>
          <div className="min-w-0">
            <div className="text-sm font-medium text-sky-100/95">最小数量跳过</div>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              <strong className="text-gray-300">默认开启</strong>：若交易所最小开仓名义大于首单意图（如设 6U，而 1 张就要 13U），则跳过该币，不会抬仓硬开。
            </p>
          </div>
        </div>

        <div className="rounded-lg border border-amber-500/40 bg-amber-950/25 px-3 py-2.5 flex items-start gap-3">
          <label className="relative inline-flex items-center cursor-pointer mt-0.5 shrink-0">
            <input type="checkbox" {...register('exclude_tradefi')} className="sr-only peer" />
            <div className="w-9 h-5 bg-gray-600 peer-checked:bg-amber-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all" />
          </label>
          <div className="min-w-0">
            <div className="text-sm font-medium text-amber-100/95">排除 TradFi / 股票永续（如 SNDK、TSLA）</div>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              <strong className="text-gray-300">默认开启</strong>：排除股票 TradFi 永续及黄金/白银/原油等非加密货币合约；已有持仓仍会管理。
            </p>
          </div>
        </div>

        <div className="rounded-lg border border-orange-500/35 bg-orange-950/20 px-3 py-2.5 flex items-start gap-3">
          <label className="relative inline-flex items-center cursor-pointer mt-0.5 shrink-0">
            <input type="checkbox" {...register('exclude_delisting')} className="sr-only peer" />
            <div className="w-9 h-5 bg-gray-600 peer-checked:bg-orange-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all" />
          </label>
          <div className="min-w-0">
            <div className="text-sm font-medium text-orange-100/95">排除快下架合约（14 天内）</div>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              依据币安合约 <code className="text-orange-200/80">exchangeInfo</code>：非 TRADING 或交割日在 14 天内不进榜、不开新仓。
            </p>
          </div>
        </div>

        {useCoinPool && (
          <div className="rounded-lg border border-sky-500/35 bg-sky-950/20 px-3 py-2.5 flex items-start gap-3">
            <label className="relative inline-flex items-center cursor-pointer mt-0.5 shrink-0">
              <input type="checkbox" {...register('exclude_mainstream')} className="sr-only peer" />
              <div className="w-9 h-5 bg-gray-600 peer-checked:bg-sky-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all" />
            </label>
            <div className="min-w-0">
              <div className="text-sm font-medium text-sky-100/95">排除主流币（BTC/ETH 等 20 个）</div>
              <p className="text-xs text-gray-400 mt-1 leading-relaxed">
                <strong className="text-gray-300">默认开启</strong>：选币池模式下排除 BTC、ETH、BNB、SOL 等主流币；固定交易对不受限；已有持仓仍会管理。
              </p>
            </div>
          </div>
        )}

        {useCoinPool && (
          <div className="rounded-lg border border-violet-500/35 bg-violet-950/20 px-3 py-2.5 flex items-start gap-3">
            <label className="relative inline-flex items-center cursor-pointer mt-0.5 shrink-0">
              <input type="checkbox" {...register('exclude_funding')} className="sr-only peer" />
              <div className="w-9 h-5 bg-gray-600 peer-checked:bg-violet-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all" />
            </label>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-violet-100/95">资金费率过滤（最近结算费率）</div>
              <p className="text-xs text-gray-400 mt-1 leading-relaxed">
                <strong className="text-gray-300">默认关闭</strong>：仅选币池模式生效；已有持仓仍会管理。
                {direction === 'long'
                  ? ' 做多：费率高于阈值时不开新仓（多头付钱一侧）。'
                  : ' 做空：费率低于阈值时不开新仓（空头付钱一侧）。'}
              </p>
              {excludeFunding && (
                <div className="mt-2 max-w-xs">
                  <label className={labelClass}>
                    {direction === 'long' ? '费率高于 (%) 不开新仓' : '费率低于 (%) 不开新仓'}
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    {...register('funding_rate_threshold_pct', { valueAsNumber: true })}
                    className={inputClass}
                    placeholder={direction === 'long' ? '0 表示 >0% 即过滤' : '0 表示 <0% 即过滤'}
                  />
                  <span className="text-xs text-gray-600">
                    单位：上一档结算费率 %（周期因合约而异）；默认 0（做多过滤正费率，做空过滤负费率）
                  </span>
                </div>
              )}
            </div>
          </div>
        )}

        {!useCoinPool && (
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className={labelClass}>交易对</label>
              <input {...register('symbol')} className={inputClass} placeholder="例如: BTCUSDT" />
            </div>
          </div>
        )}

        {useCoinPool && (
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className={labelClass}>选币池来源</label>
              <select {...register('coin_pool_source')} className={inputClass}>
                <option value="both">涨幅榜 + 跌幅榜（各取前N，合计最多2N）</option>
                <option value="gainers">仅涨幅榜</option>
                <option value="losers">仅跌幅榜</option>
              </select>
            </div>
            <div>
              <label className={labelClass}>选币间隔</label>
              <select {...register('coin_pool_refresh_seconds', { valueAsNumber: true })} className={inputClass}>
                {COIN_POOL_REFRESH_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              <span className="text-xs text-gray-600">默认1小时；重启/改参后按上次选币时间继续</span>
            </div>
            <div>
              <label className={labelClass}>开选方式</label>
              <select {...register('coin_pool_fetch_mode')} className={inputClass}>
                <option value="immediate">启动时立即抓取</option>
                <option value="interval">按间隔开选</option>
                <option value="scheduled">指定时间开选</option>
              </select>
              <span className="text-xs text-gray-600">立即抓取仅在手点「启动」时生效；重启/改参不重选</span>
            </div>
            {fetchMode === 'scheduled' && (
              <div>
                <label className={labelClass}>首次开选时间(北京时间)</label>
                <input
                  type="time"
                  step={60}
                  {...register('coin_pool_anchor_time')}
                  className={inputClass}
                />
                <span className="text-xs text-gray-600">可填任意时刻，如 00:00、08:15；仅计划时刻更新选币池</span>
              </div>
            )}
            <div>
              <label className={labelClass}>抓取前几名</label>
              <input type="number" min={1} max={50} {...register('coin_pool_top_n', { valueAsNumber: true })} className={inputClass} />
              <span className="text-xs text-gray-600">
                {coinPoolSource === 'both'
                  ? 'both：每侧前 N（默认20→涨20+跌20=40）；再经成交量/排除过滤后可能更少'
                  : '默认20，最多50；再经成交量/排除过滤后可能更少'}
              </span>
            </div>
            <div>
              <label className={labelClass}>最低 24h 成交量（万 USDT）</label>
              <input
                type="number"
                min={0}
                step={0.1}
                {...register('coin_pool_min_volume_24h', { valueAsNumber: true })}
                className={inputClass}
                placeholder="0"
              />
              <span className="text-xs text-gray-600">
                0=不限制；低于该值的币不进本策略选币池（{direction === 'long' ? '做多' : '做空'}策略独立配置）
              </span>
            </div>
          </div>
        )}

        <div className="border-t border-gray-800 my-3" />

        <h4 className="text-sm font-semibold text-gray-300">马丁格尔加仓设置</h4>
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>价格跌幅 (%)</label>
            <input type="number" step="0.1" {...register('price_drop_pct', { valueAsNumber: true })} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>跌幅倍数</label>
            <input type="number" step="0.1" {...register('price_drop_multiplier', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">每层递增，1=固定跌幅</span>
          </div>
          <div>
            <label className={labelClass}>加仓倍数</label>
            <input type="number" step="0.1" {...register('martingale_mult', { valueAsNumber: true })} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>最大加仓次数</label>
            <input
              type="number"
              min={1}
              max={200}
              step={1}
              {...register('max_layers', { valueAsNumber: true })}
              className={inputClass}
            />
            {errors.max_layers && <p className={errorClass}>{errors.max_layers.message}</p>}
          </div>
        </div>

        {signalSource === 'wick_spike' ? (
          <div className="space-y-3">
            <div>
              <label className={labelClass}>接针加仓模式</label>
              <select {...register('wick_martingale_mode')} className={inputClass}>
                <option value="price_and_wt">涨跌幅 + WT 确认</option>
                <option value="price_drop">仅涨跌幅</option>
              </select>
              <span className="text-xs text-gray-600">
                {wickMartingaleMode === 'price_and_wt'
                  ? '默认：先满足价格跌幅，再用下方 WT 参数做金叉/死叉确认'
                  : '仅按相对上一层入场价的跌幅加仓'}
              </span>
            </div>
            {wickMartingaleMode === 'price_and_wt' && (
              <div className="grid grid-cols-2 gap-3">
                <div className="col-span-2 text-xs text-gray-500">
                  以下 WT 参数用于接针加仓确认（须先满足价格跌幅）
                </div>
                <div>
                  <label className={labelClass}>WT 通道长度</label>
                  <input type="number" {...register('wt_channel_length', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">WT1周期，默认10</span>
                </div>
                <div>
                  <label className={labelClass}>WT 均线长度</label>
                  <input type="number" {...register('wt_average_length', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">WT2平滑周期，默认21</span>
                </div>
                <div>
                  <label className={labelClass}>WT 超买线</label>
                  <input type="number" step="1" {...register('wt_ob_level', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">死叉+WT1高于此值确认空向加仓，默认60</span>
                </div>
                <div>
                  <label className={labelClass}>WT 超卖线</label>
                  <input type="number" step="1" {...register('wt_os_level', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">金叉+WT1低于此值确认多向加仓，默认-60</span>
                </div>
              </div>
            )}
          </div>
        ) : (
          <div>
            <label className={`${labelClass} flex items-center gap-2`}>
              <span>马丁加仓信号确认</span>
              <label className="relative inline-flex items-center cursor-pointer">
                <input type="checkbox" {...register('martingale_rsi_enabled')} className="sr-only peer" />
                <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
              </label>
            </label>
            <span className="text-xs text-gray-600">
              开启后，加仓时仍需满足当前信号条件（RSI/WaveTrend），防止反向加仓
            </span>
          </div>
        )}

        {signalSource === 'trend_wt' && martingaleRsiEnabled && (
          <div>
            <label className={`${labelClass} flex items-center gap-2`}>
              <span>加仓叠加超级趋势过滤</span>
              <label className="relative inline-flex items-center cursor-pointer">
                <input type="checkbox" {...register('martingale_st_filter_enabled')} className="sr-only peer" />
                <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
              </label>
            </label>
            <span className="text-xs text-gray-600">默认关闭：加仓只看 WT；开启后加仓也需 15m+30m（可改）超级趋势同向</span>
          </div>
        )}

        <div className="border-t border-gray-800 my-3" />

        <h4 className="text-sm font-semibold text-gray-300">出场设置</h4>
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>止盈 (%)</label>
            <input type="number" step="0.1" {...register('take_profit_pct', { valueAsNumber: true })} className={inputClass} />
          </div>
          <div>
            <label className={`${labelClass} flex items-center gap-2`}>
              <span>止损开关</span>
              <label className="relative inline-flex items-center cursor-pointer">
                <input type="checkbox" {...register('stop_loss_enabled')} className="sr-only peer" />
                <div className="w-9 h-5 bg-gray-600 peer-checked:bg-red-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
              </label>
            </label>
          </div>
          <div>
            <label className={labelClass}>止损 (%)</label>
            <input type="number" step="0.1" {...register('stop_loss_pct', { valueAsNumber: true })} className={inputClass} disabled={!stopLossEnabled} />
            <span className="text-xs text-gray-600">{stopLossEnabled ? '按均价跌幅止损' : '止损已禁用'}</span>
          </div>
          <div>
            <label className={`${labelClass} flex items-center gap-2`}>
              <span>单币止损</span>
              <label className="relative inline-flex items-center cursor-pointer">
                <input type="checkbox" {...register('single_symbol_stop_loss_enabled')} className="sr-only peer" />
                <div className="w-9 h-5 bg-gray-600 peer-checked:bg-red-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
              </label>
            </label>
          </div>
          <div>
            <label className={labelClass}>单币止损 (%)</label>
            <input
              type="number"
              step="0.1"
              {...register('single_symbol_stop_loss_pct', { valueAsNumber: true })}
              className={inputClass}
              disabled={!singleSymbolStopLossEnabled}
            />
            <span className="text-xs text-gray-600">
              {singleSymbolStopLossEnabled
                ? '单币浮亏达保证金余额该比例时平仓并拉黑（=币安 App 保证金余额）'
                : '单币止损已禁用'}
            </span>
          </div>
          <div>
            <label className={`${labelClass} flex items-center gap-2`}>
              <span>止盈方式</span>
              <label className="relative inline-flex items-center cursor-pointer">
                <input type="checkbox" {...register('take_profit_limit_order')} className="sr-only peer" />
                <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
              </label>
              <span className="text-xs text-gray-500">{watch('take_profit_limit_order') ? '限价单' : '市价单'}</span>
            </label>
          </div>
        </div>

        <div className="rounded-lg border border-emerald-500/35 bg-emerald-950/20 px-3 py-2.5 flex items-start gap-3">
          <label className="relative inline-flex items-center cursor-pointer mt-0.5 shrink-0">
            <input type="checkbox" {...register('trailing_tp_enabled')} className="sr-only peer" />
            <div className="w-9 h-5 bg-gray-600 peer-checked:bg-emerald-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all" />
          </label>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-emerald-100/95">时间移动止盈</div>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              开仓后5分钟内达到止盈阈值则激活毫秒级移动追踪，超时回退限价止盈；开关关闭按原逻辑运行。
            </p>
            {trailingTpEnabled && (
              <div className="mt-2 grid grid-cols-3 gap-3">
                <div>
                  <label className={labelClass}>激活窗口(秒)</label>
                  <input type="number" step="1" {...register('trailing_tp_window_sec', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">默认 300=5 分钟</span>
                </div>
                <div>
                  <label className={labelClass}>基础回撤 %</label>
                  <input type="number" step="0.1" {...register('trailing_tp_drawdown_base_pct', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">盈利&lt;阶梯1 时生效；默认 30</span>
                </div>
                <div>
                  <label className={labelClass}>阶梯1 回撤 %</label>
                  <input type="number" step="0.1" {...register('trailing_tp_drawdown_tier1_pct', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">盈利≥阶梯1 收紧至此；默认 20</span>
                </div>
                <div>
                  <label className={labelClass}>阶梯2 回撤 %</label>
                  <input type="number" step="0.1" {...register('trailing_tp_drawdown_tier2_pct', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">盈利≥阶梯2 进一步收紧；默认 15</span>
                </div>
                <div>
                  <label className={labelClass}>阶梯1 阈值 %</label>
                  <input type="number" step="0.1" {...register('trailing_tp_tier1_threshold', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">默认 2.5</span>
                </div>
                <div>
                  <label className={labelClass}>阶梯2 阈值 %</label>
                  <input type="number" step="0.1" {...register('trailing_tp_tier2_threshold', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">默认 5.0</span>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>保证金阈值 (USDT)</label>
            <input type="number" step="0.01" {...register('margin_threshold', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">保证金余额低于此值时停止策略并平仓</span>
          </div>
          <div>
            <label className={labelClass}>合约杠杆</label>
            <input type="number" {...register('leverage', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">默认10x；首单开仓前自动调用币安 set_leverage</span>
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onCancel} className="px-4 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 rounded-lg">取消</button>
          <button type="submit" className="px-4 py-1.5 text-sm bg-blue-600 hover:bg-blue-700 rounded-lg font-medium">
            {initialData ? '保存修改' : '创建策略'}
          </button>
        </div>
      </form>
    </div>
  );
}
