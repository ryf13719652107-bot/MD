# CLAUDE.md

本文件为 Claude Code 在此仓库中工作提供指导。

## 项目概述

智能对冲马丁 — 币安 USDT-M 永续合约交易机器人。双向持仓模式：一个交易所账户同时运行两个策略（一个做多、一个做空）。使用 WaveTrend 或 RSI 信号，配合马丁格尔加仓和限价止盈单。

**技术栈**：FastAPI (Python 3.11) + SQLite (aiosqlite) + React/TypeScript (Vite) + ccxt (币安 USDM 合约)

**运行环境**：东京服务器 `8.211.153.248`，后端 8000 端口，前端 dev 5173 / prod 由 FastAPI 直接托管。国内开发机通过 Vite 代理连接到东京服务器。

## 构建与运行

```bash
# 后端（开发）
cd backend && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 前端（开发，Vite 代理到东京服务器）
cd frontend && npm run dev

# 前端（生产构建）
cd frontend && npm run build    # 输出到 frontend/dist/

# TypeScript 类型检查
cd frontend && npx tsc --noEmit

# 测试
cd backend && python -m pytest tests/ -v

# 服务器一键部署
bash deploy.sh
```

## 环境配置

### 后端 `.env`（`backend/.env`）
```
DATABASE_URL=sqlite+aiosqlite:///data/trading_bot.db
ENCRYPTION_KEY=          # 留空则自动生成并持久化到 backend/.encryption_key
BINANCE_API_KEY=
BINANCE_SECRET=
BINANCE_TESTNET=true
CORS_ORIGINS=http://8.211.153.248:8000
HTTP_PROXY=               # 国内开发机连币安主网需要代理，格式 http://host:port
```

### 前端 `.env.local`（`frontend/.env.local`）
```
VITE_UI_OWNER_PASSWORD=Rao1314520.
VITE_UI_GUEST_PASSWORD=Rz123456.
# VITE_UI_AUTH_DISABLED=true   # 开发用，跳过登录
```

### 加密密钥
API Key 使用 Fernet 加密存储。密钥解析顺序：`settings.encryption_key` → 环境变量 `ENCRYPTION_KEY` → 自动生成并持久化到 `backend/.encryption_key`。`mask_key()` 显示前4+后4字符。

## 架构

### 应用启动（main.py lifespan，6 步）
1. `init_db()` — 建表 + 内联 ALTER TABLE 迁移 + NULL 回填
2. `scheduler.start()` — 启动 APScheduler
3. 注册每小时整点权益快照 cron（北京时间）+ 延迟 4s 引导快照
4. `resume_running_strategies()` — 恢复 DB 中 `status=running` 的策略
5. `get_public_binance()` + coin pool 配置同步 + `start_auto_refresh()`
6. 延迟 3s 引导刷新选币池

**关闭**：`scheduler.stop()` → `coin_pool.stop_auto_refresh()` → `kline_stream.shutdown()`

### 日志
`RotatingFileHandler` → `backend/logs/bot.log`（10MB×5），同时输出控制台。`httpx`/`httpcore`/`urllib3` 日志静默为 WARNING。

### 调度时序（两个独立任务）
每个策略在 APScheduler 中有两个任务：
- **00秒（K线收盘）**：两阶段 tick — 构建 `TickContext` → 有仓币串行 `manage_symbol` → 无仓币 `evaluate_signal` 扫信号 → 有信号币 **账户级并行开仓**（`Semaphore(3)`/账户，API 并行 + DB 串行 commit）
- **30秒（K线中段）**：止盈检测 — 查询限价止盈单是否已成交，不执行任何交易
- 最大 10 策略并发（`_STRATEGY_SEMAPHORE`，控制同一时刻 tick 并行数，非策略总数上限）
- tick 开头每账户一次：`ensure_markets_loaded()` → `fetch_positions()` 写入 `TickContext.exchange_legs`（杠杆缓存在预热后跨 tick 保留，开仓命中 ≈0ms）
- **杠杆预热**：策略启动 + 选币池刷新后，`leverage_prewarm.py` 对池内/持仓 symbol 批量 `set_symbol_leverage`（同 tick 开仓走缓存 ≈0ms）
- tick 末尾对账：`asyncio.create_task(_sync_account_background)` 不阻塞；同账户 `account_sync_lock` 防并发写库

### 信号 → 交易 流程
1. 调度器在 K 线收盘边界触发
2. 选币池过滤：若启用 `exclude_tradefi`，从 pool 列表中筛掉 TradFi 永续
3. **阶段 1a**：有本地持仓的币串行 `manage_symbol()` — 拉 K 线、算信号、马丁/止盈/止损
4. **阶段 1b**：无持仓且允许新开仓的币 `evaluate_signal()` — 仅用 `TickContext` 做 TradFi/下架/主流/费率过滤（无重复 API），拉 K 线算 WT/RSI，**不下单**
5. **对账恢复**：DB 无持仓但 tick 快照 `exchange_legs` 有同向仓时，`_reconcile_orphan_from_exchange()` 用 `raw_exchange_positions` 恢复
6. 信号引擎（`strategy_engine.py`）：WaveTrend（LazyBear v5 实现）或 RSI；仍用 `_klines_for_confirmed_signal_only` 收盘 K 确认
7. **阶段 2**：`execute_open_api()` 并行（账户 `Semaphore(3)`）→ `execute_open_db()` 串行 commit
8. 马丁引擎（`martingale_engine.py`）：计算止盈价、均价、加仓条件
9. 马丁加仓顺序：先下单 → 写 DB → 取消旧止盈单 → 挂新止盈单
10. **逐币种提交**：manage/evaluate 阶段每币 commit；开仓 DB 阶段每单 commit

### 核心服务
- **`binance_service.py`**：ccxt 封装，TTL 缓存（30min）。`hedge_mode` 决定是否发送 `positionSide`/`reduceOnly`。`_format_symbol()` 将 `BTCUSDT` 转为 `BTC/USDT:USDT`。`get_public_binance()` 始终主网。TradFi 列表缓存（1h TTL）：`get_cached_tradefi_symbols()` → `fetch_tradefi_perpetual_symbols_raw()` 调 `fapiPublicGetExchangeInfo` 筛 `contractType == "TRADIFI_PERPETUAL"`。
- **`position_manager.py`**：核心交易逻辑。调度器调用 `manage_symbol()` / `evaluate_signal()` / `execute_open_api()` + `execute_open_db()`；`process_symbol()` 为兼容薄封装。`TickContext` 在 tick 级共享 exclude/费率/交易所快照。模块级工具：`_norm_sym()`、`_position_opened_at_from_exchange()`。
- **`scheduler.py`**：策略生命周期、保证金阈值、10 策略 tick 并发 + 账户级下单 `Semaphore(3)`。每 tick：构建 `TickContext` → 两阶段信号/开仓 → 后台 `create_task` sync。`_execute_tp_check()` 负责 mid-candle 止盈检测。
- **`sync_service.py`**：每 60 秒对账 DB ↔ 交易所。按 `(symbol, side)` 分组（`by_leg`），多层马丁共用一次 TP 订单查询。`_exit_price_from_tp_orders()` 查止盈单成交价，`_order_filled()` 宽松判定（`closed`/`filled` 或有 `filled>0` 且非活跃状态）。
- **`strategy_engine.py`**：`calculate_wavetrend()` — 纯 Pine Script v5 LazyBear 实现。`generate_wt_signal()` 检查金叉/死叉 + 超买超卖区。`calculate_rsi()` 使用 Wilder 平滑。
- **`martingale_engine.py`**：马丁仓位计算 — `base × multiplier^layer`，止盈价/均价/加仓触发条件。
- **`websocket_manager.py`**：单例管理所有 WS 连接。dashboard 频道用单独的 async task 每 60s 广播一次 `request_update` 快照（非每连接轮询），前端收到后调 REST `/api/dashboard`。
- **`kline_stream.py`**：策略信号用的 K 线缓存。每个 `(symbol, timeframe)` 启一个后台 `watch_ohlcv` 协程把推送写入内存缓冲；首次订阅 REST 灌种子；`get()` 提供最近 N 根快照；**条数不足或检测到缓冲区时间停滞**时 **REST 纠偏**再合并；15min 无人读取自动停订阅。后端 `lifespan` 关停时调用 `shutdown()` 释放所有 WS 任务。
- **`backup_service.py`**：历史成交 append-only JSONL 备份（`backend/data/backups/trades.jsonl`）。每条 `Trade` 落库后在 **已获得主键之后** 再写入（`commit` 后或 `flush` 后），保证备份里 `id` 与 DB 一致；恢复时按账户过滤。
- **`coin_pool_service.py`**：选币池核心。管理自动刷新循环、策略间配置同步、两个来源（涨幅/跌幅榜）隔离。`get_effective_pool_entries()` 应用 top_n → 成交量 → symbol 排除三级过滤。`sync_config_from_running_strategies()` 从运行中策略聚合配置。
- **`equity_snapshot_job.py`**：每小时整点（北京时间）为每个账户 upsert `AccountBalanceSnapshot`。
- **`strategy_flags.py`**：`exclude_delisting_enabled()`（属性为 NULL 默认 True）、`normalize_coin_pool_source()`（NULL → "gainers"）。
- **`log_service.py`**：`StrategyLogService` 单例。每个 strategy_id 内存环形缓冲区（最多 200 条）。
- **`risk_manager.py`**：风控框架 — 最大总持仓 20、单币最大 8 层、最大敞口 50%、保证金阈值。**当前未被调用**，预留后续集成。

### API 路由总览

| 路由模块 | 前缀 | 关键端点 |
|---|---|---|
| `routers/account.py` | `/api/accounts` | CRUD，删除时三阶段清理（停策略→平交易所仓位→清6张表） |
| `routers/strategies.py` | `/api/strategies` | CRUD + start/stop/panic-close + 日志 + 交易所仓位查询 + 有效选币池 |
| `routers/positions.py` | `/api/positions` | 列表/手动平仓/撤销止盈单 |
| `routers/trades.py` | `/api/trades` | 列表/删除/备份统计/恢复/CSV导出 |
| `routers/dashboard.py` | `/api/dashboard` | 60s TTL 缓存的仪表盘快照 |
| `routers/coin_pool.py` | `/api/coin-pool` | 选币池列表/刷新/配置/测试获取 |
| `routers/equity.py` | `/api/equity` | 权益曲线数据/重置基准 |
| `routers/websocket.py` | `/ws/market`, `/ws/dashboard` | 实时行情 + 仪表盘推送 |
| `main.py` 内联 | `/api/health`, `/api/klines`, `/api/ticker`, `/api/logs`, `/api/bot/toggle` | 健康检查/K线/行情/日志/主开关 |

### 历史交易备份与 REST（`/api/trades`）
- **备份写入**：调度/平仓/手工平仓/同步等对 `Trade` 写入的路径调用 `backup_trade()`（见 `position_manager`、`scheduler`、`sync_service`、`routers/positions`、`routers/strategies` 等），须在 **持久化拿到 `id` 之后** 再调（与 DB 事务顺序一致）。
- **按账户隔离**：`GET /backup-stats?account_id=`、`POST /restore?account_id=` 仅统计/恢复该 `account_id` 的行；库里 `DELETE /api/trades?account_id=` **只删该账户** 的交易行（**整库清空已取消**）。
- **路由顺序**：`DELETE ""`（按 `account_id` 批量删）必须声明在 `DELETE /{trade_id}` **之前**，避免 Starlette 错配动态路由。
- **恢复**：JSONL 时间字段在 `routers/trades.py` 内解析为 naive `datetime`；主键已存在则跳过；`IntegrityError` 返回 400。

### 数据库
- SQLite + aiosqlite，启动时 `Base.metadata.create_all()` + `init_db()` 内联 ALTER TABLE 迁移 + NULL `opened_at` 回填
- **外键约束**：`database.py` 引擎级 `@event.listens_for(engine.sync_engine, "connect")` 确保每个连接 `PRAGMA foreign_keys=ON`，CASCADE/SET NULL 真实生效
- 模型：Strategy（35+列）、Position（按层级）、Trade（已平仓，`close_reason` 含 take_profit/stop_loss/manual/panic_close/sync）、Account、CoinPool（`UniqueConstraint("symbol", "source")`）、BotConfig（键值存储）、AccountBalanceSnapshot（`UniqueConstraint("account_id", "snapshot_at")`）、AccountEquityBaseline（每账户一行）
- 所有时间存储为无时区的北京时间（`now_beijing()`）
- `Position.tp_limit_order_id`：挂限价止盈单时设置，成交/取消后置空

### 账户删除
- **`DELETE /api/accounts/{id}`** 三阶段清理：
  1. 停掉该账户所有运行中策略的调度任务，清空内存日志
  2. **平掉该账户在币安的所有仓位**（API 失效不阻塞，只记 warning）
  3. 显式逐表 `DELETE FROM` 清理 6 张表（AccountBalanceSnapshot、AccountEquityBaseline、Position、Trade、Strategy、Account），CASCADE 作为兜底
- `equity_curve.py` 的 FK 已补 `ondelete="CASCADE"`，与其余模型一致

### 对账与仓位恢复
- **孤儿仓位恢复**：`_reconcile_orphan_from_exchange()` 在两处触发：(1) DB 无持仓时查交易所；(2) 开仓前发现交易所已有同向仓位时
- **防多策略冲突**：按 `(symbol, side)` 查其他策略是否占用，一多一空反方向不冲突
- **恢复操作**：解析交易所 `entry_price`/`mark_price`/`opened_at` → 用马丁引擎重算止盈价 → `_bind_tp_limit_from_open_orders()` 关联已有限价止盈单
- **紧急平仓**：先平交易所所有仓位；若交易所已空 DB 还有幽灵仓位也一并清理（按账户匹配，不限策略）

### Dashboard 缓存
- 余额 + 交易所持仓 60s TTL 缓存（`_dashboard_exchange_cache`），按 `account_id` 分片
- REST `/api/dashboard`：命中缓存直接用，未命中调 `_fetch_dashboard_exchange_slice()` 拉新数据
- WS `/ws/dashboard`：由 `WebSocketManager._dashboard_snapshot_loop()` 单例 60s 广播，前端收到 snapshot 后调 REST

### 数据库迁移
- **内联迁移**（`database.py` `init_db()`）：ALTER TABLE 添加 `wt_ob_level`、`wt_os_level`、`exclude_tradefi`、`exclude_delisting`、`coin_pool_min_volume_24h`、`opened_at`、`closed_at` 等列，忽略已存在错误
- **回填**：`UPDATE positions SET opened_at = datetime('now') WHERE opened_at IS NULL`，`UPDATE strategies SET exclude_delisting=1 WHERE exclude_delisting IS NULL`
- **文件迁移**（`db_migrations/coin_pool_unique.py`）：将 coin_pool 唯一约束从 `symbol` 迁移为 `(symbol, source)` 复合约束（SQLite 表重建方式）

### 前端
- React 19 + TypeScript 5.7 + Vite 6 + TailwindCSS 4 + Zustand 5 + React Router 7 + lightweight-charts 4
- **认证**：角色分离 — `owner`（全部路由）vs `guest`（只读：仪表盘/持仓/交易历史）。`LoginModal` + `RoleRoute` 守卫，密码存在 `sessionStorage`。密码为空时禁止登录。
- **4 个 Zustand stores**：`authStore`（角色+登录）、`dashboardStore`（仪表盘数据+selectedAccountId）、`marketStore`（选中币种/K线周期/tickers）、`settingsStore`（主题）
- **路由**：`/`(仪表盘)、`/chart/:symbol?`(K线图)、`/strategies`(策略列表)、`/strategies/:id`(策略详情)、`/positions`(持仓)、`/trades`(交易历史)、`/coin-pool`(选币池)、`/settings`(设置)。修改类路由仅 owner 可访问。
- 生产环境：FastAPI 在 8000 端口直接托管 `frontend/dist/`，`index.html` 禁止缓存，SPA 兜底路由
- Vite 开发代理：`/api` → `http://8.211.153.248:8000`，`/ws` → `ws://8.211.153.248:8000`
- `__FRONTEND_BUILD_STAMP__`：vite 构建时注入时间戳，持仓页显示用于确认包版本
- 持仓页 `buildRows()`：以交易所数据为主构建行，匹配合并 DB 层数/止盈单/开仓时间，标注"仅交易所"行
- Dashboard 顶栏显示**累计**（非当日）多空比，右侧分栏展示当日/累计盈亏、胜率
- **顶栏账户与全局 store**：`StatusBar` 在 `listAccounts` 解析出默认/记忆账户后，必须同步 `useDashboardStore.setSelectedAccountId`（含 `null`），与本地 `useState` 一致；策略列表、交易历史、备份统计/恢复/清空均依赖 store 中的 `selectedAccountId`。
- **交易历史页**：列表、`backup` 操作、`DELETE` 批量清空均带当前选中 `account_id`；无选中账户时「清空/恢复」不可用。API 错误在 `api.ts` 的 `request()` 中格式化（含 422 `detail` 数组）。

### 测试
10 个测试文件（`backend/tests/`）：`test_strategy_engine.py`、`test_martingale_engine.py`、`test_kline_stream.py`、`test_confirmed_klines.py`、`test_coin_pool_volume_filter.py`、`test_coin_pool_source_isolation.py`、`test_delisting_filter.py`、`test_non_crypto_exclusions.py`、`test_strategy_flags.py`。运行：`cd backend && python -m pytest tests/ -v`。

## 关键约定

- **双向持仓模式**：每笔订单含 `positionSide`（"LONG"/"SHORT"），平仓加 `reduceOnly`。单向模式账户不发送这些参数。`_order_params()` 通过 `self.hedge_mode` 控制。
- **限价止盈成交检测**：mid-candle 任务（+30s）+ Sync 两套机制。`check_tp_fills()` 2s 超时查 `fetch_order`，Sync 按 leg 分组共享查询。成交价优先用 `average`，fallback 到 `info.avgPrice`/`averagePrice`/`price`。
- **成交量**：开仓和加仓用 `order.get("filled")` 实际成交量。
- **符号标准化**：比较时统一去 `/`、`:USDT`、大写。函数：`_norm_sym()`（position_manager）、`_norm_leg_symbol()`（sync_service）、`_panic_symbol_key()`（strategies）。
- **TradFi 过滤**：`exclude_tradefi` 策略级开关，默认开启。两层过滤 — 选币池在调度器筛 + `TickContext.exclude_norm` 在 `evaluate_signal` 筛。已有持仓的 TradFi 币不会被抛弃。
- **下架过滤**：`exclude_delisting` 策略级开关，默认开启。筛掉 14 天内即将下架的合约。
- **新增数据库列**：同步添加 model + schema + 前端 types + `init_db()` 迁移 + NULL 兜底。
- **历史备份与库表**：页面「清空」只删 SQLite `trades` 中当前账户；JSONL 仍追加保留，需删文件才能在备份侧移除记录。
- **Python 命令**：Windows 开发机使用 `python`；东京服务器使用 `python3`。服务器上 venv 路径为 `backend/.venv/`。
- **服务器命令**：东京服务器用 `systemctl restart trading-bot` 管理后端进程，`deploy.sh` 自动检测。
- **Git 身份**：本仓库配置 `user.name "ryf13719652107"` / `user.email "ryf13719652107@gmail.com"`，勿改全局 git config。
- **部署教程**：`部署教程.md` 包含推送、服务器一键部署、单独更新后端/前端、首次初始化、运维命令。
