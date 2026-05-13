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
2. 选币池过滤：若启用 `exclude_tradefi`，从 pool 列表中筛掉 TradFi 永续
3. PositionManager 逐币种处理：拉取 K 线（WS 流缓冲优先，REST 兜底，详见 `kline_stream.py`）、生成信号、检查已有持仓
4. **TradFi 过滤**：`process_symbol()` 入口先查 `exclude_tradefi`，若币种在 TradFi 集合且无已有持仓则直接跳过（不查 K 线不生成信号）
5. **对账恢复**：DB 无持仓但交易所有仓位时，`_reconcile_orphan_from_exchange()` 从交易所恢复记录。按策略方向认领，防多策略冲突
6. 信号引擎（`strategy_engine.py`）：WaveTrend（LazyBear v5 实现）或 RSI
7. 马丁引擎（`martingale_engine.py`）：计算止盈价、均价、加仓条件
8. 开仓前实时 `fetch_positions` 查交易所防重复；若交易所已有仓位则触发对账恢复而非开新仓
9. 马丁加仓顺序：先下单 → 写 DB → 取消旧止盈单 → 挂新止盈单
10. **逐币种提交**：每个 symbol 处理完单独 commit，一个失败回滚该币不影响其他币

### 核心服务
- **`binance_service.py`**：ccxt 封装，TTL 缓存（30min）。`hedge_mode` 决定是否发送 `positionSide`/`reduceOnly`。`_format_symbol()` 将 `BTCUSDT` 转为 `BTC/USDT:USDT`。`get_public_binance()` 始终主网。TradFi 列表缓存（1h TTL）：`get_cached_tradefi_symbols()` → `fetch_tradefi_perpetual_symbols_raw()` 调 `fapiPublicGetExchangeInfo` 筛 `contractType == "TRADIFI_PERPETUAL"`。
- **`position_manager.py`**：核心交易逻辑。`process_symbol()` 总入口，含 TradFi 过滤、对账恢复、信号生成、开仓、持仓管理、马丁加仓、TP 检测。模块级工具：`_norm_sym()`、`_position_opened_at_from_exchange()`（从 `timestamp`/`updateTime`/`entryTime` 解析开仓时间）。
- **`scheduler.py`**：策略生命周期、保证金阈值、5 并发信号量。`lifespan` 启动时 `resume_running_strategies()` 为 DB 中 `status=running` 的策略重新挂载定时任务（`stopped` 不动）；每 tick 拉 pool symbols → TradFi 过滤 → 逐币种 `process_symbol()` → commit。`_execute_tp_check()` 负责 mid-candle 止盈检测。
- **`sync_service.py`**：每 60 秒对账 DB ↔ 交易所。按 `(symbol, side)` 分组（`by_leg`），多层马丁共用一次 TP 订单查询。`_exit_price_from_tp_orders()` 查止盈单成交价，`_order_filled()` 宽松判定（`closed`/`filled` 或有 `filled>0` 且非活跃状态）。
- **`strategy_engine.py`**：`calculate_wavetrend()` — 纯 Pine Script v5 LazyBear 实现。`generate_wt_signal()` 检查金叉/死叉 + 超买超卖区。`calculate_rsi()` 使用 Wilder 平滑。
- **`websocket_manager.py`**：单例管理所有 WS 连接。dashboard 频道用单独的 async task 每 60s 广播一次 `request_update` 快照（非每连接轮询），前端收到后调 REST `/api/dashboard`。
- **`kline_stream.py`**：策略信号用的 K 线缓存。每个 `(symbol, timeframe)` 启一个后台 `watch_ohlcv` 协程把推送写入内存缓冲；首次订阅 REST 灌种子；`get()` 提供最近 N 根快照；**条数不足或检测到缓冲区时间停滞**（WS 挂了仍当缓存够新）时 **REST 纠偏**，再合并；15min 无人读取自动停订阅。后端 `lifespan` 关停时调用 `shutdown()` 释放所有 WS 任务。
- **`backup_service.py`**：历史成交 append-only JSONL 备份（`backend/data/backups/trades.jsonl`）。每条 `Trade` 落库后在 **已获得主键之后** 再写入（`commit` 后或 `flush` 后），保证备份里 `id` 与 DB 一致；恢复时按账户过滤。删除备份：停服务后删该文件或清空文件即可，无单独 API。

### 历史交易备份与 REST（`/api/trades`）
- **备份写入**：调度/平仓/手工平仓/同步等对 `Trade` 写入的路径调用 `backup_trade()`（见 `position_manager`、`scheduler`、`sync_service`、`routers/positions`、`routers/strategies` 等），须在 **持久化拿到 `id` 之后** 再调（与 DB 事务顺序一致）。
- **按账户隔离**：`GET /backup-stats?account_id=`、`POST /restore?account_id=` 仅统计/恢复该 `account_id` 的行；库里 `DELETE /api/trades?account_id=` **只删该账户** 的交易行（**整库清空已取消**）。
- **路由顺序**：`DELETE ""`（按 `account_id` 批量删）必须声明在 `DELETE /{trade_id}` **之前**，避免 Starlette 错配动态路由。
- **恢复**：JSONL 时间字段在 `routers/trades.py` 内解析为 naive `datetime`；主键已存在则跳过；`IntegrityError` 返回 400。

### 数据库
- SQLite + aiosqlite，启动时 `Base.metadata.create_all()` + `init_db()` 内联 ALTER TABLE 迁移 + NULL `opened_at` 回填
- 模型：Strategy（含 `exclude_tradefi`）、Position（按层级）、Trade（已平仓记录）、Account、CoinPool、BotConfig
- 所有时间存储为无时区的北京时间（`now_beijing()`）
- `Position.tp_limit_order_id`：挂限价止盈单时设置，成交/取消后置空

### 对账与仓位恢复
- **孤儿仓位恢复**：`_reconcile_orphan_from_exchange()` 在两处触发：(1) DB 无持仓时查交易所；(2) 开仓前发现交易所已有同向仓位时
- **防多策略冲突**：按 `(symbol, side)` 查其他策略是否占用，`RECONCILE_SKIPPED_OTHER_STRATEGY` 跳过。一多一空反方向不冲突
- **恢复操作**：解析交易所 `entry_price`/`mark_price`/`opened_at` → 用马丁引擎重算止盈价 → `_bind_tp_limit_from_open_orders()` 关联已有限价止盈单
- **紧急平仓**：先平交易所所有仓位；若交易所已空 DB 还有幽灵仓位也一并清理（按账户匹配，不限策略）

### Dashboard 缓存
- 余额 + 交易所持仓 60s TTL 缓存（`_dashboard_exchange_cache`），按 `account_id` 分片
- REST `/api/dashboard`：命中缓存直接用，未命中调 `_fetch_dashboard_exchange_slice()` 拉新数据
- WS `/ws/dashboard`：不再每连接自己轮询，由 `WebSocketManager._dashboard_snapshot_loop()` 单例 60s 广播，前端收到 snapshot 后调 REST

### 前端
- React + TypeScript + Vite + TailwindCSS + Zustand
- 生产环境：FastAPI 在 8000 端口直接托管 `frontend/dist/`，`index.html` 禁止缓存
- `__FRONTEND_BUILD_STAMP__`：vite 构建时注入时间戳，持仓页显示用于确认包版本
- 持仓页 `buildRows()`：以交易所数据为主构建行，匹配合并 DB 层数/止盈单/开仓时间，标注"仅交易所"行
- Dashboard 顶栏显示**累计**（非当日）多空比，右侧分栏展示当日/累计盈亏、胜率
- **顶栏账户与全局 store**：`StatusBar` 在 `listAccounts` 解析出默认/记忆账户后，必须同步 `useDashboardStore.setSelectedAccountId`（含 `null`），与本地 `useState` 一致；策略列表、交易历史、备份统计/恢复/清空均依赖 store 中的 `selectedAccountId`。
- **交易历史页**：列表、`backup` 操作、`DELETE` 批量清空均带当前选中 `account_id`；无选中账户时「清空/恢复」不可用。API 错误在 `api.ts` 的 `request()` 中格式化（含 422 `detail` 数组）。

## 关键约定

- **双向持仓模式**：每笔订单含 `positionSide`（"LONG"/"SHORT"），平仓加 `reduceOnly`。单向模式账户不发送这些参数。`_order_params()` 通过 `self.hedge_mode` 控制。
- **限价止盈成交检测**：mid-candle 任务（+30s）+ Sync 两套机制。`check_tp_fills()` 2s 超时查 `fetch_order`，Sync 按 leg 分组共享查询。成交价优先用 `average`，fallback 到 `info.avgPrice`/`averagePrice`/`price`。
- **成交量**：开仓和加仓用 `order.get("filled")` 实际成交量。
- **符号标准化**：比较时统一去 `/`、`:USDT`、大写。函数：`_norm_sym()`（position_manager）、`_norm_leg_symbol()`（sync_service）、`_panic_symbol_key()`（strategies）。
- **TradFi 过滤**：`exclude_tradefi` 策略级开关，默认关闭。两层过滤 — 选币池在调度器筛 + 逐币种在 `process_symbol` 入口筛。已有持仓的 TradFi 币不会被抛弃。
- **新增数据库列**：同步添加 model + schema + 前端 types + `init_db()` 迁移 + NULL 兜底。
- **历史备份与库表**：页面「清空」只删 SQLite `trades` 中当前账户；JSONL 仍追加保留，需删文件才能在备份侧移除记录。
- **Python 命令**：Windows 环境使用 `python`（非 `python3.11`）。
