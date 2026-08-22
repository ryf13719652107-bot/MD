import logging
import traceback
from datetime import datetime
from sqlalchemy import String, Float, Integer, Boolean, DateTime, ForeignKey, Index, event
from ..config import now_beijing
from sqlalchemy.orm import Mapped, mapped_column
from ..database import Base

logger = logging.getLogger(__name__)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # 'long' or 'short'
    symbol: Mapped[str] = mapped_column(String(50), nullable=True)  # NULL = use coin pool

    # Signal source
    signal_source: Mapped[str] = mapped_column(String(20), default="wavetrend", server_default="wavetrend")  # 'rsi' | 'wavetrend' | 'trend_wt' | 'martingale_base' | 'wick_spike'

    # Wick spike (毫秒接针) params — only used when signal_source == wick_spike
    wick_volume_mult: Mapped[float] = mapped_column(Float, default=6.0, server_default="6.0")
    wick_volume_sma_period: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    wick_atr_period: Mapped[int] = mapped_column(Integer, default=14, server_default="14")
    wick_spike_atr_mult: Mapped[float] = mapped_column(Float, default=4.0, server_default="4.0")
    wick_cooldown_sec: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # progress 量能放宽（默认开）：刺破后按 progress 线性把量能收到 relax_mult
    wick_amp_vol_relax_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    wick_vol_relax_progress_start: Mapped[float] = mapped_column(
        Float, default=1.0, server_default="1.0"
    )
    wick_vol_relax_progress_full: Mapped[float] = mapped_column(
        Float, default=1.5, server_default="1.5"
    )
    wick_vol_relax_mult: Mapped[float] = mapped_column(
        Float, default=5.0, server_default="5.0"
    )
    # 本根相对开盘最小涨跌幅 %（接针方向）；0=关闭
    wick_min_move_pct: Mapped[float] = mapped_column(
        Float, default=3.0, server_default="3.0"
    )
    # 开盘→极值回撤占比上限 %；超过则跳过；0=关闭
    wick_max_retrace_pct: Mapped[float] = mapped_column(
        Float, default=50.0, server_default="50.0"
    )
    # 刺破后等量窗口（秒）；0=关闭武装
    wick_arm_wait_sec: Mapped[float] = mapped_column(
        Float, default=12.0, server_default="12.0"
    )
    # 武装时量不够，确认阶段前 N 秒免回撤；0=不放宽
    wick_arm_retrace_grace_sec: Mapped[float] = mapped_column(
        Float, default=5.0, server_default="5.0"
    )
    # grace 免回撤时 tip_gap% 上限；0=不限制
    wick_arm_grace_max_tip_gap_pct: Mapped[float] = mapped_column(
        Float, default=2.0, server_default="2.0"
    )
    # 反弹追踪（方案J）：confirm后等价格从针尖反弹触发市价，追踪真针尖
    wick_rebound_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    # 1m 开盘 vs EMA30：空开盘<EMA不做空；多开盘>EMA不做多
    wick_ema25_filter_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    # 反弹占针深%触发市价（开盘100针尖90，深10%，20%=92触发）
    wick_rebound_trigger_pct: Mapped[float] = mapped_column(
        Float, default=20.0, server_default="20.0"
    )
    # 反弹占针深%放弃（超过说明不是针是反转）
    wick_rebound_abort_pct: Mapped[float] = mapped_column(
        Float, default=35.0, server_default="35.0"
    )
    # confirm后等反弹超时秒数；0=本根内不超时（换根未反弹仍超时并锁新根）
    wick_rebound_wait_sec: Mapped[float] = mapped_column(
        Float, default=0.0, server_default="0.0"
    )
    # 接针加仓模式：price_drop=仅涨跌幅；price_and_wt=涨跌幅+WT确认（默认）
    wick_martingale_mode: Mapped[str] = mapped_column(
        String(32), default="price_and_wt", server_default="price_and_wt"
    )

    # 时间移动止盈（开关关闭=按原限价止盈逻辑运行，零影响）
    # 开仓后 window_sec 内达到 take_profit_pct → 激活毫秒级追踪；超时则回退限价止盈
    trailing_tp_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    # 激活窗口（秒），默认 300=5分钟
    trailing_tp_window_sec: Mapped[float] = mapped_column(
        Float, default=300.0, server_default="300.0"
    )
    # 基础回撤比例 %（盈利<tier1 时生效）
    trailing_tp_drawdown_base_pct: Mapped[float] = mapped_column(
        Float, default=30.0, server_default="30.0"
    )
    # 盈利≥tier1_threshold 时收紧至此回撤比例 %
    trailing_tp_drawdown_tier1_pct: Mapped[float] = mapped_column(
        Float, default=20.0, server_default="20.0"
    )
    # 盈利≥tier2_threshold 时进一步收紧至此回撤比例 %
    trailing_tp_drawdown_tier2_pct: Mapped[float] = mapped_column(
        Float, default=15.0, server_default="15.0"
    )
    # 阶梯1触发盈利 %（默认 2.5）
    trailing_tp_tier1_threshold: Mapped[float] = mapped_column(
        Float, default=2.5, server_default="2.5"
    )
    # 阶梯2触发盈利 %（默认 5.0）
    trailing_tp_tier2_threshold: Mapped[float] = mapped_column(
        Float, default=5.0, server_default="5.0"
    )

    # General params
    rsi_period: Mapped[int] = mapped_column(Integer, default=14)
    timeframe: Mapped[str] = mapped_column(String(10), default="1m")
    margin_threshold: Mapped[float] = mapped_column(Float, default=0.0)  # Auto-stop below this margin

    # WaveTrend params
    wt_channel_length: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    wt_average_length: Mapped[int] = mapped_column(Integer, default=21, server_default="21")
    wt_ob_level: Mapped[float] = mapped_column(Float, default=60.0, server_default="60.0")
    wt_os_level: Mapped[float] = mapped_column(Float, default=-60.0, server_default="-60.0")

    # Supertrend filter params (trend_wt only)
    st_atr_period: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    st_factor: Mapped[float] = mapped_column(Float, default=3.0, server_default="3.0")
    st_timeframe_1: Mapped[str] = mapped_column(String(10), default="15m", server_default="15m")
    st_timeframe_2: Mapped[str] = mapped_column(String(10), default="30m", server_default="30m")

    # Entry position params
    base_qty_type: Mapped[str] = mapped_column(String(20), default="margin_pct")  # 'margin_pct' or 'usdt'
    base_qty_value: Mapped[float] = mapped_column(Float, default=6.0)  # 6% margin or USDT amount
    # True: 交易所最小开仓名义 > 意图名义时跳过（不抬仓、不报错硬开）
    skip_min_qty_exceeds: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    rsi_entry_threshold: Mapped[float] = mapped_column(Float, default=30.0)  # long=30, short=75

    # Martingale params
    price_drop_pct: Mapped[float] = mapped_column(Float, default=30.0)
    price_drop_multiplier: Mapped[float] = mapped_column(Float, default=1.0)  # 鍔犱粨璺屽箙鍊嶆暟锛氭瘡灞傞€掑 price_drop_pct * multiplier^(layer-1)
    martingale_mult: Mapped[float] = mapped_column(Float, default=1.5)
    max_layers: Mapped[int] = mapped_column(Integer, default=8)
    martingale_rsi_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")  # Require entry signal for martingale adds
    # trend_wt only: when confirming martingale adds, also require Supertrend filter (default off = plain WT)
    martingale_st_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    # Take profit params
    take_profit_pct: Mapped[float] = mapped_column(Float, default=2.0)
    take_profit_limit_order: Mapped[bool] = mapped_column(Boolean, default=True)

    # Stop loss
    stop_loss_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=5.0)
    single_symbol_stop_loss_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    single_symbol_stop_loss_pct: Mapped[float] = mapped_column(Float, default=10.0, server_default="10")

    # Slippage protection
    slippage_pct: Mapped[float] = mapped_column(Float, default=0.5)  # Max slippage %, 0 = disabled

    # Leverage
    leverage: Mapped[int] = mapped_column(Integer, default=10)  # Contract leverage

    # Coin pool
    use_coin_pool: Mapped[bool] = mapped_column(Boolean, default=True)
    coin_pool_source: Mapped[str] = mapped_column(String(20), default="gainers")  # gainers, losers, both
    coin_pool_refresh_seconds: Mapped[int] = mapped_column(Integer, default=3600)  # how often to refresh coin pool
    coin_pool_fetch_mode: Mapped[str] = mapped_column(String(20), default="interval")  # 'immediate' | 'interval' | 'scheduled'
    coin_pool_anchor_hour: Mapped[int] = mapped_column(Integer, default=8, server_default="8")
    coin_pool_anchor_minute: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    coin_pool_schedule_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # scheduled 妯″紡閰嶇疆鐢熸晥鏃堕棿
    coin_pool_top_n: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    # 鏈€浣?24h 鎴愪氦棰濓紙USDT quoteVolume锛夛紱0 = 涓嶉檺鍒躲€備粎鏈瓥鐣ヨ繃婊ら€夊竵姹犳柊寮€浠撳垪琛?
    coin_pool_min_volume_24h: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    # True: 鎺掗櫎 TRADIFI_PERPETUAL + 榛勯噾鐧介摱鍘熸补绛夐潪鍔犲瘑璐у竵鍚堢害
    exclude_tradefi: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # True: 鎺掗櫎 14 澶╁唴涓嬫灦鎴栭潪 TRADING 鐨勫悎绾︼紙鏃犱粨鏃朵笉寮€鏂板崟锛?
    exclude_delisting: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # True: 閫夊竵姹犳ā寮忎笅鎺掗櫎 BTC/ETH 绛変富娴佸竵锛堝浐瀹氫氦鏄撳涓嶅彈闄愶級
    exclude_mainstream: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # True: 閫夊竵姹犳ā寮忎笅鎸夎祫閲戣垂鐜囪繃婊ゆ柊寮€浠擄紙宸叉湁鎸佷粨浠嶇鐞嗭級
    exclude_funding: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 鏈€杩戠粨绠楄祫閲戣垂鐜囬槇鍊?%): 鍋氬 rate>闃堝€?杩囨护锛涘仛绌?rate<闃堝€?杩囨护锛涢粯璁?0
    funding_rate_threshold_pct: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")

    # Runtime state
    status: Mapped[str] = mapped_column(String(20), default="stopped")  # 'running', 'stopped', 'error'
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_rsi: Mapped[float] = mapped_column(Float, nullable=True)
    last_signal: Mapped[str] = mapped_column(String(20), nullable=True)  # 'long', 'short', 'neutral'
    last_signal_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing, onupdate=now_beijing)


Index("idx_strategies_account", Strategy.account_id)
Index("idx_strategies_status", Strategy.status)


@event.listens_for(Strategy, "before_update")
def _track_status_change(mapper, connection, target):
    # 仅调试 status 变更；任何异常都吞掉，避免污染 async flush（MissingGreenlet）
    try:
        state = target._sa_instance_state
        hist = state.get_history("status", True)
        if hist.has_changes() and hist.deleted and hist.deleted[0] != target.status:
            logger.warning(
                "STATUS CHANGE: strategy_id=%d '%s' -> '%s'\n%s",
                target.id, hist.deleted[0], target.status,
                "".join(traceback.format_stack()[-8:-1]),
            )
    except Exception:
        pass


