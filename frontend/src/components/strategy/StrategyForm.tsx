import { useEffect, useState, type ReactNode } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { ChevronDown } from 'lucide-react';
import InlineNotice from '../ui/InlineNotice';
import ConfirmDialog from '../ui/ConfirmDialog';
import {
  COIN_POOL_REFRESH_OPTIONS,
  formatAnchorTime,
  nearestCoinPoolRefreshSeconds,
  parseAnchorTime,
  type Strategy,
  type StrategyApiPayload,
  type StrategyFormData,
} from '../../types/strategy';
import { snapshotTemplatePatch, type StrategyParamTemplate } from '../../types/strategyTemplates';
import { api } from '../../services/api';
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
  wick_arm_wait_sec: z.number().min(-1).max(120),
  wick_arm_retrace_grace_sec: z.number().min(0).max(60),
  wick_arm_grace_max_tip_gap_pct: z.number().min(0).max(20),
  wick_rebound_enabled: z.boolean(),
  wick_ema25_filter_enabled: z.boolean(),
  wick_rebound_trigger_pct: z.number().min(0).max(100),
  wick_rebound_abort_pct: z.number().min(0).max(100),
  wick_rebound_wait_sec: z.number().min(0).max(60),
  wick_martingale_mode: z.enum(['price_drop', 'price_and_wt']),
  wick_volume_mode: z.enum(['original', 'instant_early', 'real_only']),
  wick_instant_active_until_pct: z.number().min(0).max(1),
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
  slippage_pct: z.number().min(0).max(10),
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
  onSubmit: (data: StrategyApiPayload) => void | Promise<void>;
  onCancel: () => void;
}

function FormSection({
  title,
  defaultOpen = true,
  hint,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  hint?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="border border-gray-800 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 text-left hover:bg-gray-800/50"
      >
        <span className="text-sm font-semibold text-gray-200">{title}</span>
        <span className="flex items-center gap-2">
          {hint && <span className="text-[11px] text-gray-500 font-normal">{hint}</span>}
          <ChevronDown size={16} className={`text-gray-500 transition-transform ${open ? 'rotate-180' : ''}`} />
        </span>
      </button>
      <div className={open ? 'px-3 pb-3 space-y-3' : 'hidden'}>{children}</div>
    </section>
  );
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
      wick_arm_wait_sec: initialData.wick_arm_wait_sec ?? 0,
      wick_arm_retrace_grace_sec: initialData.wick_arm_retrace_grace_sec ?? 5,
      wick_arm_grace_max_tip_gap_pct: initialData.wick_arm_grace_max_tip_gap_pct ?? 2,
      wick_rebound_enabled: initialData.wick_rebound_enabled ?? true,
      wick_ema25_filter_enabled: initialData.wick_ema25_filter_enabled ?? true,
      wick_rebound_trigger_pct: initialData.wick_rebound_trigger_pct ?? 20,
      wick_rebound_abort_pct: initialData.wick_rebound_abort_pct ?? 35,
      wick_rebound_wait_sec: initialData.wick_rebound_wait_sec ?? 0,
      wick_martingale_mode:
        initialData.wick_martingale_mode === 'price_drop' ? 'price_drop' : 'price_and_wt',
      wick_volume_mode:
        initialData.wick_volume_mode === 'instant_early' ||
        initialData.wick_volume_mode === 'real_only'
          ? initialData.wick_volume_mode
          : 'original',
      wick_instant_active_until_pct: initialData.wick_instant_active_until_pct ?? 0.5,
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
    wick_arm_wait_sec: 0,
    wick_arm_retrace_grace_sec: 5,
    wick_arm_grace_max_tip_gap_pct: 2,
    wick_rebound_enabled: true,
    wick_ema25_filter_enabled: true,
    wick_rebound_trigger_pct: 20,
    wick_rebound_abort_pct: 35,
    wick_rebound_wait_sec: 0,
    wick_martingale_mode: 'price_and_wt',
    wick_volume_mode: 'original',
    wick_instant_active_until_pct: 0.5,
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
  const payload = {
    ...rest,
    coin_pool_refresh_seconds: nearestCoinPoolRefreshSeconds(data.coin_pool_refresh_seconds),
    coin_pool_anchor_hour: hour,
    coin_pool_anchor_minute: minute,
    coin_pool_min_volume_24h: (data.coin_pool_min_volume_24h || 0) * WAN,
  } as StrategyApiPayload;
  const cleaned = { ...payload } as Record<string, unknown>;
  for (const key of Object.keys(cleaned)) {
    const val = cleaned[key];
    if (typeof val === 'number' && !Number.isFinite(val)) {
      delete cleaned[key];
    }
  }
  return cleaned as StrategyApiPayload;
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
    register, handleSubmit, watch, setValue, getValues, reset, formState: { errors },
  } = useForm<StrategyFormData>({
    resolver: zodResolver(schema),
    defaultValues: toFormDefaults(initialData, accountOptions, defaultAccountId),
  });

  const [activeTemplateId, setActiveTemplateId] = useState<number | null>(null);
  const [templates, setTemplates] = useState<StrategyParamTemplate[]>([]);
  const [templateName, setTemplateName] = useState('');
  const [templateBusy, setTemplateBusy] = useState(false);
  const [tplNotice, setTplNotice] = useState<string | null>(null);
  const [pendingOverwrite, setPendingOverwrite] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<StrategyParamTemplate | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    document.getElementById('strategy-form')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  const loadTemplates = async () => {
    try {
      setTemplates(await api.listStrategyTemplates());
    } catch {
      /* 列表失败不挡填表 */
    }
  };

  useEffect(() => {
    loadTemplates();
  }, []);

  const applyTemplate = (tpl: StrategyParamTemplate) => {
    reset(
      { ...getValues(), ...tpl.patch },
      { keepDefaultValues: true },
    );
    setActiveTemplateId(tpl.id);
    setTemplateName(tpl.name);
  };

  const saveCurrentAsTemplate = async (overwrite = false) => {
    const name = templateName.trim();
    if (!name) {
      setTplNotice('请输入模板名称');
      return;
    }
    const exists = templates.find((t) => t.name === name);
    if (exists && !overwrite) {
      setPendingOverwrite(true);
      return;
    }
    try {
      setTemplateBusy(true);
      setTplNotice(null);
      const saved = await api.saveStrategyTemplate(name, snapshotTemplatePatch(getValues()));
      await loadTemplates();
      setActiveTemplateId(saved.id);
      setPendingOverwrite(false);
      setTplNotice(overwrite ? `已覆盖模板「${name}」` : `已保存模板「${name}」`);
    } catch (e: any) {
      setTplNotice(`保存模板失败：${e?.message || e}`);
    } finally {
      setTemplateBusy(false);
    }
  };

  const deleteTemplate = async () => {
    const tpl = pendingDelete;
    if (!tpl) return;
    try {
      setTemplateBusy(true);
      await api.deleteStrategyTemplate(tpl.id);
      if (activeTemplateId === tpl.id) setActiveTemplateId(null);
      await loadTemplates();
      setPendingDelete(null);
      setTplNotice(`已删除模板「${tpl.name}」`);
    } catch (e: any) {
      setTplNotice(`删除模板失败：${e?.message || e}`);
    } finally {
      setTemplateBusy(false);
    }
  };

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
  const wickVolumeMode = watch('wick_volume_mode', 'original');
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
    <div id="strategy-form" className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="font-semibold mb-3">{initialData ? '编辑策略' : '新建策略'}</h3>
      <div className="rounded-lg border border-blue-500/25 bg-blue-950/20 px-3 py-2.5 mb-3">
        <div className="flex items-baseline justify-between gap-3 mb-2">
          <span className="text-xs font-medium text-blue-100">参数模板</span>
          <span className="text-[11px] text-gray-500">一键套用，不改名称 / 账户</span>
        </div>
        {templates.length === 0 ? (
          <p className="text-[11px] text-gray-500 mb-2">还没有模板。调好下面参数后输入名称保存。</p>
        ) : (
          <div className="flex flex-wrap gap-1.5 mb-2">
            {templates.map((tpl) => {
              const on = activeTemplateId === tpl.id;
              return (
                <span
                  key={tpl.id}
                  className={
                    on
                      ? 'inline-flex items-center gap-1 pl-2.5 pr-1 py-0.5 rounded-md text-xs font-medium bg-blue-600 text-white'
                      : 'inline-flex items-center gap-1 pl-2.5 pr-1 py-0.5 rounded-md text-xs font-medium bg-gray-800 text-gray-300 border border-gray-700'
                  }
                >
                  <button
                    type="button"
                    onClick={() => applyTemplate(tpl)}
                    className="hover:underline"
                    disabled={templateBusy}
                  >
                    {tpl.name}
                  </button>
                  <button
                    type="button"
                    title="删除模板"
                    disabled={templateBusy}
                    onClick={() => setPendingDelete(tpl)}
                    className={
                      on
                        ? 'px-1 rounded text-blue-100 hover:text-white hover:bg-blue-700'
                        : 'px-1 rounded text-gray-500 hover:text-red-300 hover:bg-gray-700'
                    }
                  >
                    ×
                  </button>
                </span>
              );
            })}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={templateName}
            onChange={(e) => { setTemplateName(e.target.value); setTplNotice(null); }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                saveCurrentAsTemplate();
              }
            }}
            maxLength={40}
            placeholder="模板名称"
            className="h-8 w-40 px-2 rounded border border-gray-700 bg-gray-800 text-xs text-gray-200"
          />
          <button
            type="button"
            onClick={() => saveCurrentAsTemplate()}
            disabled={templateBusy || !templateName.trim()}
            className="h-8 px-2.5 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-xs text-white"
          >
            保存当前参数
          </button>
        </div>
        {tplNotice && (
          <div className="mt-2">
            <InlineNotice
              kind={tplNotice.includes('失败') || tplNotice.startsWith('请') ? 'error' : 'success'}
              onClose={() => setTplNotice(null)}
            >
              {tplNotice}
            </InlineNotice>
          </div>
        )}
      </div>
      <form
        onSubmit={handleSubmit(async (data) => {
          setSubmitting(true);
          try {
            await onSubmit(toApiPayload(data));
          } finally {
            setSubmitting(false);
          }
        })}
        className="space-y-3"
      >
        {Object.keys(errors).length > 0 && (
          <InlineNotice kind="error">请检查标红的参数后再保存</InlineNotice>
        )}
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
            <p className="text-xs text-cyan-200/90">
              毫秒接针仅币安：刺破 + 放量确认后市价开仓。默认开反弹追踪（confirm 后等针尖反弹再下单）。
            </p>
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
                <span className="text-xs text-gray-600">相对开盘→极值；默认 50；填 0 关闭</span>
              </div>
            </div>
            <div>
              <label className={`${labelClass} flex items-center gap-2`}>
                <span>市价反弹追踪</span>
                <label className="relative inline-flex items-center cursor-pointer">
                  <input type="checkbox" {...register('wick_rebound_enabled')} className="sr-only peer" />
                  <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
                </label>
              </label>
              <span className="text-xs text-gray-600">confirm 后等针尖反弹到触发%再市价</span>
            </div>
            <div className="grid grid-cols-3 gap-3">
              <div>
                <label className={labelClass}>反弹触发 %</label>
                <input type="number" step="1" {...register('wick_rebound_trigger_pct', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">占针深；默认 20；0=立刻市价</span>
              </div>
              <div>
                <label className={labelClass}>反弹放弃 %</label>
                <input type="number" step="1" {...register('wick_rebound_abort_pct', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">须大于触发%；0=关闭放弃</span>
              </div>
              <div>
                <label className={labelClass}>等反弹超时(秒)</label>
                <input type="number" step="0.5" {...register('wick_rebound_wait_sec', { valueAsNumber: true })} className={inputClass} />
                <span className="text-xs text-gray-600">0=本根内不超时，换根仍超时</span>
              </div>
            </div>
            <div>
              <label className={`${labelClass} flex items-center gap-2`}>
                <span>1m 开盘 vs EMA25 过滤</span>
                <label className="relative inline-flex items-center cursor-pointer">
                  <input type="checkbox" {...register('wick_ema25_filter_enabled')} className="sr-only peer" />
                  <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
                </label>
              </label>
              <span className="text-xs text-gray-600">做空：开盘低于 EMA25 不做空；做多相反</span>
            </div>
            <FormSection title="接针进阶" defaultOpen={false} hint="量能模式 / 武装窗 / 放宽">
              <div>
                <label className={`${labelClass} flex items-center gap-2`}>
                  <span>progress 量能放宽</span>
                  <label className="relative inline-flex items-center cursor-pointer">
                    <input type="checkbox" {...register('wick_amp_vol_relax_enabled')} className="sr-only peer" />
                    <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
                  </label>
                </label>
                <span className="text-xs text-gray-600">刺破后按进度把放量倍数线性降到「放宽后量能」</span>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className={labelClass}>成交量确认模式</label>
                  <select {...register('wick_volume_mode')} className={inputClass}>
                    <option value="original">原方案（瞬时量全程）</option>
                    <option value="instant_early">方案1（前段瞬时+后段真实）</option>
                    <option value="real_only">方案3（纯真实累计量）</option>
                  </select>
                </div>
                {wickVolumeMode === 'instant_early' && (
                  <div>
                    <label className={labelClass}>瞬时量生效进度上限（0~1）</label>
                    <input type="number" step="0.05" {...register('wick_instant_active_until_pct', { valueAsNumber: true })} className={inputClass} />
                    <span className="text-xs text-gray-600">默认 0.5（1m 即前 30 秒）</span>
                  </div>
                )}
                <div>
                  <label className={labelClass}>刺破等量窗口(秒)</label>
                  <input type="number" step="1" {...register('wick_arm_wait_sec', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">&gt;0 本根内超时；0 换根才超时；-1 关闭武装</span>
                </div>
                <div>
                  <label className={labelClass}>等量免回撤(秒)</label>
                  <input type="number" step="0.5" {...register('wick_arm_retrace_grace_sec', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">武装时量不够，确认前 N 秒免回撤；默认 5</span>
                </div>
                <div>
                  <label className={labelClass}>免回撤 tip_gap% 上限</label>
                  <input type="number" step="0.1" {...register('wick_arm_grace_max_tip_gap_pct', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">grace 时离针尖过远不开；默认 2</span>
                </div>
                <div>
                  <label className={labelClass}>开始放宽 progress</label>
                  <input type="number" step="0.1" {...register('wick_vol_relax_progress_start', { valueAsNumber: true })} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>完全放宽 progress</label>
                  <input type="number" step="0.1" {...register('wick_vol_relax_progress_full', { valueAsNumber: true })} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>放宽后量能倍数</label>
                  <input type="number" step="0.1" {...register('wick_vol_relax_mult', { valueAsNumber: true })} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>同币额外冷却（秒）</label>
                  <input type="number" {...register('wick_cooldown_sec', { valueAsNumber: true })} className={inputClass} />
                  <span className="text-xs text-gray-600">默认 0（仅同币同根 K 去重）</span>
                </div>
              </div>
            </FormSection>
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

        <FormSection title="过滤" defaultOpen={false} hint="TradFi / 下架 / 主流 / 费率">
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

        </FormSection>

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

        <FormSection title="马丁格尔加仓" hint="跌幅 / 倍数 / 层数">
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
        </FormSection>

        <FormSection title="出场与风控" hint="止盈 / 止损 / 杠杆">
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
              设定时间内达到止盈阈值则一直按移动止盈追踪；超时未触发立即回退限价。开关关闭按原限价逻辑运行。
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
        </FormSection>

        <div className="sticky bottom-0 -mx-4 -mb-4 px-4 py-3 bg-gray-900/95 border-t border-gray-800 flex justify-end gap-2 backdrop-blur-sm">
          <button type="button" onClick={onCancel} className="px-4 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 rounded-lg">取消</button>
          <button type="submit" disabled={submitting} className="px-4 py-1.5 text-sm bg-blue-600 hover:bg-blue-700 disabled:opacity-50 rounded-lg font-medium">
            {submitting ? '保存中…' : initialData ? '保存修改' : '创建策略'}
          </button>
        </div>
      </form>
      <ConfirmDialog
        open={pendingOverwrite}
        title={`已有模板「${templateName.trim()}」`}
        detail="要用当前表单参数覆盖吗？"
        confirmLabel="覆盖"
        busy={templateBusy}
        onConfirm={() => saveCurrentAsTemplate(true)}
        onCancel={() => setPendingOverwrite(false)}
      />
      <ConfirmDialog
        open={pendingDelete != null}
        title={`删除模板「${pendingDelete?.name ?? ''}」？`}
        confirmLabel="删除"
        danger
        busy={templateBusy}
        onConfirm={deleteTemplate}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
