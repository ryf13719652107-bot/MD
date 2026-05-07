# CLAUDE.md

本文件为 Claude Code 在此仓库中工作提供指导。

## 项目概述

智能对冲马丁 — 币安 USDT-M 永续合约交易机器人。双向持仓模式：一个交易所账户同时运行两个策略（一个做多、一个做空）。使用 WaveTrend 或 RSI 信号，配合马丁格尔加仓和限价止盈单。

**技术栈**：FastAPI (Python 3.11) + SQLite (aiosqlite) + React/TypeScript (Vite) + ccxt (币安 USDM 合约)

## 构建与运行

```bash
# 后端（开发）
cd backend && python3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 前端（开发，Vite 代理到后端）
cd frontend && npm run dev

# 前端（生产构建）
cd frontend && npm run build    # 输出到 frontend/dist/

# TypeScript 类型检查
cd frontend && npx tsc --noEmit

# 测试
cd backend && python3.11 -m pytest tests/ -v

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
2. PositionManager 逐币种处理：拉取 K 线（含缓存）、生成信号、检查已有持仓
3. 信号引擎（`services/strategy_engine.py`）：WaveTrend（LazyBear v5 实现）或 RSI
4. 马丁引擎（`services/martingale_engine.py`）：计算止盈价、均价、加仓条件
5. 开仓前去重：实时 `fetch_positions` 查交易所（不用过期快照）
6. 马丁加仓顺序：先下单 → 写 DB → 取消旧止盈单 → 挂新止盈单
7. 订单执行经由 `binance_service.py` → ccxt `binanceusdm`

### 核心服务
- **`binance_service.py`**：ccxt 封装，TTL 缓存（10分钟）。`BinanceService.hedge_mode` 决定是否发送 `positionSide`/`reduceOnly` 参数。`_format_symbol()` 将 `BTCUSDT` 转为 `BTC/USDT:USDT`。`get_public_binance()` 始终使用主网获取排行榜数据。
- **`position_manager.py`**：核心交易逻辑 — 信号生成、开仓、持仓管理（止损/止盈/马丁加仓）、平仓。另有 `check_tp_fills()` 方法供 mid-candle 止盈检测调用。
- **`scheduler.py`**：策略生命周期、保证金阈值、5 并发信号量。`_execute_strategy()` 负责交易，`_execute_tp_check()` 负责 mid-candle 止盈检测。
- **`scheduler.py`**：策略生命周期管理、保证金阈值强制执行、最大 5 并发信号量
- **`sync_service.py`**：每 60 秒对账 DB ↔ 交易所持仓。跳过有 `tp_limit_order_id` 的持仓（留给 tick 处理）。不创建孤儿持仓或伪造交易记录。
- **`strategy_engine.py`**：`calculate_wavetrend()` — 纯 Pine Script v5 LazyBear 实现。`generate_wt_signal()` 检查金叉/死叉 + 超买超卖区。`calculate_rsi()` 使用 Wilder 平滑。

### 数据库
- SQLite + aiosqlite，启动时 `Base.metadata.create_all()` + `init_db()` 中内联 ALTER TABLE 迁移
- 模型：Strategy（策略参数）、Position（持仓，按层级）、Trade（已平仓盈亏记录）、Account（账户）、CoinPool（选币池）、BotConfig（总开关）
- 所有时间存储为无时区的北京时间（`now_beijing()`）
- `Position.tp_limit_order_id`：挂限价止盈单时设置，成交/取消后置空

### 前端
- React + TypeScript + Vite + TailwindCSS + Zustand
- 生产环境：FastAPI 在 8000 端口直接托管 `frontend/dist/` 静态文件（无需额外服务）
- 开发环境：Vite 代理 `/api` 和 `/ws` 到后端
- `useWebSocket.ts`：开发走 Vite 代理；生产直连 `hostname:8000`
- `api.ts`：开发用 `/api`（走代理）；生产用 `hostname:8000/api`
- Dashboard 通过 30 秒轮询 + WebSocket 快照触发自动刷新

### -1106 错误处理
所有平仓/紧急平仓/保证金止损路径遇到 `-1106`（单向模式拒绝 `reduceOnly`/`positionSide`）时，直接调用 `exchange.create_order()` 重试 — 不携带任何额外参数。

## 关键约定

- **双向持仓模式**：每笔订单必须包含 `positionSide`（"LONG"/"SHORT"），平仓时加 `reduceOnly`。单向模式账户不得发送这些参数。`_order_params()` 方法通过 `self.hedge_mode` 控制。
- **限价止盈成交检测**：由独立的 mid-candle 任务（+30s 偏移）负责，不阻塞交易。`check_tp_fills()` 遍历持仓查询 `fetch_order`，检查状态 `closed`/`filled`，用真实的 `average` 成交价记录 Trade。设 2 秒超时。Sync 跳过有待处理止盈单的持仓。
- **成交量**：开仓和加仓时使用 `order.get("filled")` 实际成交量，非请求量。
- **事务**：整 tick 单事务，任何币种失败回滚全部，不产生部分提交。
- **K 线不再截断**：信号计算使用完整的 K 线数据（调度器对齐到 K 线收盘时刻，最后一根 K 线是已完成的）
- **新增数据库列**：同步添加到 model + schema (Pydantic) + 前端 types/form + `init_db()` 迁移
- **服务器部署**：远端执行 `bash deploy.sh`；前端由 FastAPI 在 8000 端口托管；通过宝塔进程守护重启
