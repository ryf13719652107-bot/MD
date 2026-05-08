# CLAUDE.md

本文件为 Claude Code 在此仓库中工作提供指导。

## 项目概述

智能对冲马丁 — 币安 USDT-M 永续合约交易机器人。双向持仓模式：一个交易所账户同时运行两个策略（一个做多、一个做空）。使用 WaveTrend 或 RSI 信号，配合马丁格尔加仓和限价止盈单。

**技术栈**：FastAPI (Python 3.11) + SQLite (aiosqlite) + React/TypeScript (Vite) + ccxt (币安 USDM 合约)

## 构建与运行

```bash
# 后端（开发）
cd backend && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 前端（开发，Vite 代理到后端）
cd frontend && npm run dev

# 前端（生产构建）
cd frontend && npm run build    # 输出到 frontend/dist/

# TypeScript 类型检查
cd frontend && npx tsc --noEmit

# 测试
cd backend && python -m pytest tests/ -v

# 部署
bash deploy.sh
```

## 架构

### 调度时序（两个独立任务）
每个策略在 APScheduler 中有两个任务：
- **00秒（K线收盘）**：执行交易 — 拉 K 线、生成信号、开仓/加仓/止损/止盈检查
- **30秒（K线中段）**：止盈检测 — 查询限价止盈单是否已成交，不执行任何交易
- 最大 5 策略并发（`_STRATEGY_SEMAPHORE`）

### 信号 → 交易 流程
1. 调度器在 K 线收盘边界触发
2. PositionManager 逐币种处理：拉取 K 线（模块级缓存 30s TTL）、生成信号、检查已有持仓
3. **对账恢复**：DB 无持仓但交易所有仓位时，`_reconcile_orphan_from_exchange()` 从交易所恢复持仓记录。按策略方向认领，防多策略冲突（一多一空不同方向不冲突）
4. 信号引擎（`services/strategy_engine.py`）：WaveTrend（LazyBear v5 实现）或 RSI
5. 马丁引擎（`services/martingale_engine.py`）：计算止盈价、均价、加仓条件
6. 开仓前实时 `fetch_positions` 查交易所防重复；若交易所已有仓位则触发对账恢复
7. 马丁加仓顺序：先下单 → 写 DB → 取消旧止盈单 → 挂新止盈单
8. **逐币种提交**：每个 symbol 处理完单独 commit，一个币失败回滚该币不影响其他币（`scheduler.py` 循环体内 commit/rollback）

### 核心服务
- **`binance_service.py`**：ccxt 封装，TTL 缓存（10分钟）。`hedge_mode` 决定是否发送 `positionSide`/`reduceOnly`。`_format_symbol()` 将 `BTCUSDT` 转为 `BTC/USDT:USDT`。`get_public_binance()` 始终主网。
- **`position_manager.py`**：核心交易逻辑。`process_symbol()` 总入口，内部调用 `_open_position()`、`_manage_positions()`、`_close_positions()`、`_martingale_add()`、`check_tp_fills()`。新增对账恢复 `_reconcile_orphan_from_exchange()`（DB 丢仓时从交易所恢复）和 `_bind_tp_limit_from_open_orders()`（恢复时关联已有止盈单）。`_position_opened_at_from_exchange()` 从交易所 `timestamp`/`updateTime`/`entryTime` 解析真实开仓时间。
- **`scheduler.py`**：策略生命周期、保证金阈值、5 并发信号量。`_execute_strategy()` 负责交易，`_execute_tp_check()` 负责 mid-candle 止盈检测。逐币种提交。
- **`sync_service.py`**：每 60 秒对账 DB ↔ 交易所。按币对+方向分组（`by_leg`），多层共用一次 TP 订单查询。`_exit_price_from_tp_orders()` 查询止盈单成交价，`_order_filled()` 宽松判定成交。DB 有但交易所无的持仓标记已平仓并记录 Trade。
- **`strategy_engine.py`**：`calculate_wavetrend()` — 纯 Pine Script v5 LazyBear 实现。`generate_wt_signal()` 检查金叉/死叉 + 超买超卖区。`calculate_rsi()` 使用 Wilder 平滑。

### 数据库
- SQLite + aiosqlite，启动时 `Base.metadata.create_all()` + `init_db()` 内联 ALTER TABLE 迁移 + NULL `opened_at` 回填
- 模型：Strategy、Position（持仓按层级）、Trade（已平仓记录）、Account、CoinPool、BotConfig
- 所有时间存储为无时区的北京时间（`now_beijing()`）
- `Position.tp_limit_order_id`：挂限价止盈单时设置，成交/取消后置空
- `Position.opened_at`：开仓时间，新建时由 model default 填充；对账恢复时从交易所数据解析；历史 NULL 由 `init_db()` 和 `list_positions` 读取时双重兜底

### 对账与仓位恢复
- **孤儿仓位恢复**：`_reconcile_orphan_from_exchange()` 在 `process_symbol` 的两个时机触发：(1) DB 无持仓时查交易所；(2) 有信号要开仓但交易所已有同向仓位时
- **防多策略冲突**：按 `(symbol, side)` 查其他策略是否已占用。一多一空反方向不冲突；同账户同方向仅一个策略记账
- **恢复时**：从交易所解析 `entry_price`/`mark_price`/`opened_at`、重算止盈价、绑定已有的限价止盈单
- **紧急平仓**：关闭交易所所有仓位后，还会清理账户下所有 DB 幽灵持仓（不限策略），按账户匹配 close

### 前端
- React + TypeScript + Vite + TailwindCSS + Zustand
- 生产环境：FastAPI 在 8000 端口直接托管 `frontend/dist/`，`index.html` 禁止缓存
- 开发环境：Vite 代理 `/api` 和 `/ws` 到后端服务器
- `__FRONTEND_BUILD_STAMP__`：vite 构建时注入时间戳，持仓页显示用于确认是否新包生效
- 持仓页 `buildRows()`：以交易所数据为主构建行，匹配合并 DB 的层数/止盈单/开仓时间，图标区分"已挂单"/"未挂单"止盈状态

### -1106 错误处理
所有平仓/紧急平仓/保证金止损路径遇到 `-1106`（单向模式拒绝 `reduceOnly`/`positionSide`）时，直接调用 `exchange.create_order()` 重试 — 不携带任何额外参数。

## 关键约定

- **双向持仓模式**：每笔订单必须包含 `positionSide`（"LONG"/"SHORT"），平仓时加 `reduceOnly`。单向模式账户不得发送这些参数。`_order_params()` 通过 `self.hedge_mode` 控制。
- **限价止盈成交检测**：由独立的 mid-candle 任务（+30s 偏移）负责。`check_tp_fills()` 遍历持仓查 `fetch_order`，状态 `closed`/`filled` 即成交，用真实 `average` 成交价记录 Trade，2 秒超时。Sync 也查止盈单成交价（`_exit_price_from_tp_orders`），按 leg 分组共享查询。
- **成交量**：开仓和加仓时使用 `order.get("filled")` 实际成交量，非请求量。
- **符号标准化**：比较时统一 `.replace("/","").replace(":USDT","").upper()`，模块级函数 `_norm_sym()`/`_norm_leg_symbol()`/`_panic_symbol_key()`。
- **新增数据库列**：同步添加到 model + schema (Pydantic) + 前端 types + `init_db()` 迁移 + NULL 兜底回填。
- **服务器部署**：远端执行 `bash deploy.sh`；前端由 FastAPI 在 8000 端口托管；通过宝塔进程守护重启。
- **Python 命令**：Windows 环境使用 `python`（非 `python3.11`）。
