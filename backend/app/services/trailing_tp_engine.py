"""时间移动止盈纯逻辑引擎（无 I/O，可独立单测）。

流程（开关 trailing_tp_enabled=True 时生效；关闭则完全不进入此模块）：
  1) 开仓后写入 Position.trailing_tp_state='armed'，peak_pct=0；不挂限价
  2) armed 且窗口内盈利达到 activate_threshold（=take_profit_pct）→ 切 'active'，
     之后一直按移动止盈追踪，不再改回限价
  3) armed 且窗口到点仍未激活 → 切 'expired'，立即回退挂限价止盈
  4) active：更新 peak，回撤 ≥ 阶梯限制 → 市价平仓

阶梯回撤比例（基于峰值盈利 peak_pct）：
  peak_pct <  tier1_threshold(2.5) → drawdown_base_pct(30)
  peak_pct >= tier1_threshold(2.5) → drawdown_tier1_pct(20)
  peak_pct >= tier2_threshold(5.0) → drawdown_tier2_pct(15)

回撤比例定义 = (peak_profit - cur_profit) / peak_profit * 100
  （相对峰值盈利的比例，非相对价格；peak_profit<=0 时不触发）

毫秒级精度：本引擎所有时间参数用 ms 时间戳；调用方（wick_spike_runner）
基于 price_stream seq 变化驱动，天然毫秒级响应。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Position.trailing_tp_state 取值
STATE_ARMED = "armed"      # 开仓后窗口内等待激活
STATE_ACTIVE = "active"    # 已激活毫秒级追踪
STATE_EXPIRED = "expired"  # 窗口超时已回退限价止盈


@dataclass
class TrailingTpParams:
    """从 Strategy 表读取的参数快照（热路径只读，避免 ORM 访问）。"""
    enabled: bool
    window_sec: float
    activate_threshold_pct: float   # = strategy.take_profit_pct
    drawdown_base_pct: float         # 30
    drawdown_tier1_pct: float        # 20
    drawdown_tier2_pct: float        # 15
    tier1_threshold: float           # 2.5
    tier2_threshold: float           # 5.0


@dataclass
class TrailingTpMemState:
    """热路径内存中的单 Position 追踪状态（每 tick 更新，定期 sync 回 DB）。

    避免每 tick 查 DB；refresh_context 周期(15s) 把 mem_state 写回 Position。
    """
    position_id: int
    symbol: str
    side: str               # 'long' | 'short'
    entry_price: float
    opened_at_ms: int       # 开仓时间（毫秒时间戳）
    state: str              # STATE_ARMED / STATE_ACTIVE / STATE_EXPIRED
    peak_pct: float         # 入场后达到的最高盈利 %（用于回撤计算）
    # 激活后追踪的最高盈利价（做空=入场后最低价；做多=入场后最高价）
    peak_price: float


def profit_pct(side: str, entry_price: float, current_price: float) -> float:
    """当前盈利百分比 %。做空：entry>current 为正盈利；做多：current>entry 为正盈利。"""
    if entry_price <= 0:
        return 0.0
    d = (side or "").lower()
    if d == "short":
        return (entry_price - current_price) / entry_price * 100.0
    if d == "long":
        return (current_price - entry_price) / entry_price * 100.0
    return 0.0


def update_peak(mem: TrailingTpMemState, current_price: float) -> float:
    """更新峰值盈利 % 与峰值价；返回新的 peak_pct。

    做空：峰值价=入场后最低价（越低越盈利）；做多：峰值价=入场后最高价。
    """
    cur_p = profit_pct(mem.side, mem.entry_price, current_price)
    if cur_p > mem.peak_pct:
        mem.peak_pct = cur_p
        d = (mem.side or "").lower()
        if d == "short":
            # 做空盈利增加 = 价格下跌；峰值价取更低
            if mem.peak_price <= 0 or current_price < mem.peak_price:
                mem.peak_price = current_price
        elif d == "long":
            if mem.peak_price <= 0 or current_price > mem.peak_price:
                mem.peak_price = current_price
    return mem.peak_pct


def is_window_expired(mem: TrailingTpMemState, now_ms: int, window_sec: float) -> bool:
    """armed 状态下：是否已超过激活窗口。"""
    if mem.state != STATE_ARMED:
        return False
    return (now_ms - mem.opened_at_ms) > int(window_sec * 1000)


def should_activate(
    mem: TrailingTpMemState,
    params: TrailingTpParams,
    current_price: float,
    now_ms: int,
) -> bool:
    """armed 且窗口未到点、现价盈利已达阈值 → 应切 active。

    窗口已过：不再激活，由 apply_tick 走 expired 立即回退限价。
    """
    if mem.state != STATE_ARMED:
        return False
    if (now_ms - mem.opened_at_ms) > int(params.window_sec * 1000):
        return False
    cur_p = profit_pct(mem.side, mem.entry_price, current_price)
    return cur_p >= params.activate_threshold_pct - 1e-12


def current_drawdown_limit(params: TrailingTpParams, peak_pct: float) -> float:
    """根据峰值盈利返回当前应适用的回撤比例 %。

    peak_pct 越高，回撤容忍越小（锁利越紧）。
    """
    if peak_pct >= params.tier2_threshold - 1e-12:
        return params.drawdown_tier2_pct
    if peak_pct >= params.tier1_threshold - 1e-12:
        return params.drawdown_tier1_pct
    return params.drawdown_base_pct


def current_drawdown_pct(
    mem: TrailingTpMemState, current_price: float
) -> float:
    """当前从峰值盈利回撤了多少比例 %。

    = (peak_pct - cur_pct) / peak_pct * 100
    peak_pct<=0 时返回 0（从未盈利，谈不上回撤）。
    """
    if mem.peak_pct <= 0:
        return 0.0
    cur_p = profit_pct(mem.side, mem.entry_price, current_price)
    dd = (mem.peak_pct - cur_p) / mem.peak_pct * 100.0
    return max(0.0, dd)


def should_close(
    mem: TrailingTpMemState,
    params: TrailingTpParams,
    current_price: float,
) -> bool:
    """active 状态下：当前回撤超过适用限制 → 平仓。"""
    if mem.state != STATE_ACTIVE:
        return False
    if mem.peak_pct <= 0:
        return False
    dd = current_drawdown_pct(mem, current_price)
    limit = current_drawdown_limit(params, mem.peak_pct)
    return dd >= limit - 1e-12


def remaining_window_sec(
    mem: TrailingTpMemState, now_ms: int, window_sec: float
) -> float:
    """armed 状态下距离窗口结束的剩余秒数（<0 表示已超时）。"""
    elapsed_ms = now_ms - mem.opened_at_ms
    return window_sec - elapsed_ms / 1000.0


def apply_tick(
    mem: TrailingTpMemState,
    params: TrailingTpParams,
    current_price: float,
    now_ms: int,
) -> Optional[str]:
    """热路径主入口：每 tick 调用，返回事件名（供 runner 打日志/触发后续动作）。

    返回值：
      None            — 无事件
      'activated'     — 窗口内达阈值，armed→active，之后一直追踪
      'window_expired'— 窗口到点仍未激活，armed→expired，立即回退限价
      'close'         — active 触发回撤平仓，调用方应市价平仓
    """
    if not params.enabled:
        return None

    if mem.state == STATE_ARMED:
        # armed 期间也持续更新 peak，激活时基线是历史最高而不是激活当刻。
        update_peak(mem, current_price)
        if should_activate(mem, params, current_price, now_ms):
            mem.state = STATE_ACTIVE
            return "activated"
        if is_window_expired(mem, now_ms, params.window_sec):
            mem.state = STATE_EXPIRED
            return "window_expired"
        return None

    if mem.state == STATE_ACTIVE:
        update_peak(mem, current_price)
        if should_close(mem, params, current_price):
            return "close"
        return None

    # expired：已回退限价，本引擎不再管
    return None
