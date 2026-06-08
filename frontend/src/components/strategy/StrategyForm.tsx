import { useEffect } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import type { Strategy, StrategyApiPayload, StrategyFormData } from '../../types/strategy';
import type { Account } from '../../types';

const schema = z.object({
  account_id: z.number().min(1, '请选择账户'),
  name: z.string().min(1, '请输入策略名称').max(100),
  direction: z.enum(['long', 'short']),
  symbol: z.string().optional().or(z.literal('')),
  signal_source: z.enum(['rsi', 'wavetrend', 'martingale_base']),
  rsi_period: z.number().min(5).max(50),
  timeframe: z.enum(['1m', '5m', '15m', '1h']),
  wt_channel_length: z.number().min(2).max(50),
  wt_average_length: z.number().min(2).max(100),
  wt_ob_level: z.number().min(10).max(100),
  wt_os_level: z.number().min(-100).max(-10),
  margin_threshold: z.number().min(0),
  base_qty_type: z.enum(['margin_pct', 'usdt']),
  base_qty_value: z.number().min(0.01),
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
  take_profit_pct: z.number().min(0.1).max(50),
  take_profit_limit_order: z.coerce.boolean(),
  stop_loss_enabled: z.coerce.boolean(),
  stop_loss_pct: z.number().min(0.1).max(100),
  leverage: z.number().min(1).max(125),
  use_coin_pool: z.coerce.boolean(),
  coin_pool_source: z.enum(['gainers', 'losers', 'both']),
  coin_pool_refresh_hours: z.number().min(1).max(24),
  coin_pool_fetch_mode: z.enum(['immediate', 'interval', 'scheduled']),
  coin_pool_anchor_hour: z.number().min(0).max(23),
  coin_pool_top_n: z.number().min(1).max(50),
  /** 表单内以「万 USDT」录入，提交时 ×1e4 转为 USDT */
  coin_pool_min_volume_24h: z.number().min(0).max(99999999),
  exclude_tradefi: z.coerce.boolean(),
  exclude_delisting: z.coerce.boolean(),
  exclude_mainstream: z.coerce.boolean(),
  exclude_funding: z.coerce.boolean(),
  funding_rate_threshold_pct: z.number().min(-5).max(5),
});

const WAN = 1e4;

interface Props {
  accounts: Account[];
  initialData: Strategy | null;
  onSubmit: (data: StrategyApiPayload) => void;
  onCancel: () => void;
}

function toFormDefaults(initialData: Strategy | null, accounts: Account[]): StrategyFormData {
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
      margin_threshold: initialData.margin_threshold,
      base_qty_type: initialData.base_qty_type,
      base_qty_value: initialData.base_qty_value,
      rsi_entry_threshold: initialData.rsi_entry_threshold,
      price_drop_pct: initialData.price_drop_pct,
      price_drop_multiplier: initialData.price_drop_multiplier ?? 1,
      martingale_mult: initialData.martingale_mult,
      max_layers: Number(initialData.max_layers ?? 8),
      martingale_rsi_enabled: initialData.martingale_rsi_enabled ?? false,
      take_profit_pct: initialData.take_profit_pct,
      take_profit_limit_order: initialData.take_profit_limit_order,
      stop_loss_enabled: initialData.stop_loss_enabled ?? true,
      stop_loss_pct: initialData.stop_loss_pct,
      slippage_pct: initialData.slippage_pct ?? 0.5,
      leverage: initialData.leverage ?? 10,
      use_coin_pool: initialData.use_coin_pool,
      coin_pool_source: initialData.coin_pool_source,
      coin_pool_refresh_hours: Math.max(1, Math.round((initialData.coin_pool_refresh_seconds ?? 3600) / 3600)),
      coin_pool_fetch_mode:
        initialData.coin_pool_fetch_mode === 'scheduled' || initialData.coin_pool_fetch_mode === 'immediate'
          ? initialData.coin_pool_fetch_mode
          : 'interval',
      coin_pool_anchor_hour: initialData.coin_pool_anchor_hour ?? 8,
      coin_pool_top_n: initialData.coin_pool_top_n ?? 20,
      coin_pool_min_volume_24h: (initialData.coin_pool_min_volume_24h ?? 0) / WAN,
      exclude_tradefi: initialData.exclude_tradefi ?? true,
      exclude_delisting: initialData.exclude_delisting ?? true,
      exclude_mainstream: initialData.exclude_mainstream ?? true,
      exclude_funding: initialData.exclude_funding ?? false,
      funding_rate_threshold_pct: initialData.funding_rate_threshold_pct ?? 0,
    };
  }
  return {
    account_id: accounts[0]?.id || 0,
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
    margin_threshold: 0,
    base_qty_type: 'margin_pct',
    base_qty_value: 6,
    rsi_entry_threshold: 30,
    price_drop_pct: 30,
    price_drop_multiplier: 1,
    martingale_mult: 1.5,
    max_layers: 8,
    martingale_rsi_enabled: false,
    take_profit_pct: 2,
    take_profit_limit_order: true,
    stop_loss_enabled: false,
    stop_loss_pct: 5,
    slippage_pct: 0.5,
    leverage: 10,
    use_coin_pool: true,
    coin_pool_source: 'gainers',
    coin_pool_refresh_hours: 1,
    coin_pool_fetch_mode: 'interval',
    coin_pool_anchor_hour: 8,
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
  const { coin_pool_refresh_hours, ...rest } = data;
  return {
    ...rest,
    coin_pool_refresh_seconds: Math.round(coin_pool_refresh_hours * 3600),
    coin_pool_min_volume_24h: (data.coin_pool_min_volume_24h || 0) * WAN,
  };
}

export default function StrategyForm({ accounts, initialData, onSubmit, onCancel }: Props) {
  const {
    register, handleSubmit, watch, setValue, getValues, formState: { errors },
  } = useForm<StrategyFormData>({
    resolver: zodResolver(schema),
    defaultValues: toFormDefaults(initialData, accounts),
  });

  const direction = watch('direction', 'long');
  const signalSource = watch('signal_source', 'rsi');
  const useCoinPool = watch('use_coin_pool', true);
  const fetchMode = watch('coin_pool_fetch_mode', 'interval');
  const stopLossEnabled = watch('stop_loss_enabled', true);
  const excludeFunding = watch('exclude_funding', false);

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
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.name} {a.testnet ? '(测试网)' : '(实盘)'}</option>)}
              </select>
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
              <option value="martingale_base">基础马丁（每根K线开盘开首单）</option>
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
            <span className="text-xs text-gray-600">按K线收盘后执行</span>
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

        {signalSource === 'wavetrend' && (
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
                <option value="both">涨幅榜 + 跌幅榜</option>
                <option value="gainers">仅涨幅榜</option>
                <option value="losers">仅跌幅榜</option>
              </select>
            </div>
            <div>
              <label className={labelClass}>选币间隔(小时)</label>
              <input type="number" min={1} max={24} step={1} {...register('coin_pool_refresh_hours', { valueAsNumber: true })} className={inputClass} />
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
                <select {...register('coin_pool_anchor_hour', { valueAsNumber: true })} className={inputClass}>
                  {Array.from({ length: 24 }, (_, h) => (
                    <option key={h} value={h}>{String(h).padStart(2, '0')}:00</option>
                  ))}
                </select>
                <span className="text-xs text-gray-600">仅计划时刻更新选币池；交易照常只使用池内币种</span>
              </div>
            )}
            <div>
              <label className={labelClass}>抓取前几名</label>
              <input type="number" min={1} max={50} {...register('coin_pool_top_n', { valueAsNumber: true })} className={inputClass} />
              <span className="text-xs text-gray-600">默认20，最多50</span>
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

        <div>
          <label className={`${labelClass} flex items-center gap-2`}>
            <span>马丁加仓RSI确认</span>
            <label className="relative inline-flex items-center cursor-pointer">
              <input type="checkbox" {...register('martingale_rsi_enabled')} className="sr-only peer" />
              <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
            </label>
          </label>
          <span className="text-xs text-gray-600">开启后，加仓时RSI仍需满足入场条件，防止反向加仓</span>
        </div>

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
            <span className="text-xs text-gray-600">{stopLossEnabled ? '止损已启用' : '止损已禁用'}</span>
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

        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>保证金阈值 (USDT)</label>
            <input type="number" step="0.01" {...register('margin_threshold', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">低于此值自动停止策略</span>
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
