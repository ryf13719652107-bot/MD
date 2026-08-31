# CLAUDE.md

本文件为 Claude Code 在此仓库中工作提供指导。

## 项目概述

智能对冲马丁 — 币安 / Gate.io USDT-M 永续合约交易机器人。双向持仓模式：一个交易所账户同时运行两个策略（一个做多、一个做空）。信号源含 WaveTrend、RSI、趋势 WT、基础马丁、毫秒接针（`wick_spike`，仅币安），配合马丁格尔加仓和限价止盈单。

**技术栈**：FastAPI (Python 3.11) + SQLite (aiosqlite) + React/TypeScript (Vite) + ccxt（币安 USDM + Gate 永续）

**运行环境**：东京服务器 `8.211.153.248`，后端 8000 端口；生产前端由 FastAPI 托管 `frontend/dist/`（一般不单独跑 5173）。国内开发机通过 Vite 代理连接到东京服务器。

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

# 服务器更新代码（不启停进程；进程由守护自行重启）
bash deploy.sh
```

`deploy.sh`：`git pull` → pip → `alembic upgrade head` → `npm run build`。**不会** `systemctl`/`pkill`/`nohup` 启停进程。无新迁移时 alembic 几乎空跑，可每次保留。

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

### 应用启动（main.py lifespan）
1. `init_db()` — 建表 + 内联 ALTER TABLE 迁移 + NULL 回填 + coin_pool 唯一约束迁移
2. `scheduler.start()` — 启动 APScheduler
3. 注册每小时整点权益快照 cron（北京时间 `:00:25`）+ **启动后约 4s 引导快照**（`_equity_bootstrap_snap`，重启会多一个非整点点位）
4. `resume_running_strategies()` — 恢复 DB 中 `status=running` 的策略（含 `wick_spike` runner）
5. 预热 `get_public_binance()` / `get_public_gate()` + coin pool 配置同步 + `start_auto_refresh()`
6. 延迟引导刷新选币池（按运行中账户所属交易所分别刷新）

**关闭**：`scheduler.stop()` → `coin_pool.stop_auto_refresh()` → `kline_stream.shutdown()` / price stream 等

### 交易所路由（`exchange_factory.py`）
- `normalize_exchange_id` / `account_exchange_id` → `binance` | `gate`
- `get_public_exchange(ex)` / `get_exchange_for_account(account)` 按所返回客户端
- **`extract_margin_balance(client, balance)`**：币安 App「保证金余额」=`totalMarginBalance`（缺字段时用钱包+未实现推导）；Gate 用合约权益。用于：收益曲线小时快照、保证金阈值止损、单币止损分母、顶栏余额
- **`extract_dashboard_balances`**：返回 `(钱包余额, 保证金余额)` — 币安 `totalWalletBalance` / `totalMarginBalance`
- 旧名 `extract_wallet_balance` = `extract_margin_balance`（**不是** App「钱包余额」）

### 日志
`RotatingFileHandler` → `backend/logs/bot.log`（10MB×5），同时输出控制台。`httpx`/`httpcore`/`urllib3` 日志静默为 WARNING。接针近失/武装/触发另打 `wick_spike ...` 行（含 `kline_vol`/`trade_vol`/`vol_now`/`sma`），前端「接针统计 / 查币种监控」解析此文件。

### 调度时序
每个策略在 APScheduler 中有独立任务（按信号源略有差异）：
- **00秒（K线收盘）**：两阶段 tick — 构建 `TickContext` → 有仓币串行 `manage_symbol` → 无仓币 `evaluate_signal` → 有信号币 **账户级并行开仓**（`Semaphore(3)`/账户）
- **30秒（K线中段）**：止盈检测 — 查限价止盈是否成交（`check_tp_fills`）
- **`wick_spike`**：仅币安；`WickSpikeRunner` 价流毫秒开仓；`:00` tick **跳过新开仓**；止盈仍 +30s；持仓管理另有 +40s 任务；Gate 账户启动拒绝
- tick 开头按**策略所属账户交易所**取 `get_public_exchange(exchange_id)` + 该所选币池
- 杠杆预热、后台 sync 等同前

### 信号 → 交易 流程
1. 调度器触发（收盘或接针流）
2. 选币池：按账户 `exchange` 取池；`exclude_tradefi` / 下架 / 主流 / 费率等过滤
3. **阶段 1a**：有仓 `manage_symbol()` — 马丁/止盈/止损（含单币止损，分母=保证金余额）
4. **阶段 1b**：无仓 `evaluate_signal()` — 算信号不下单
5. 孤儿仓恢复、`execute_open_api` / `execute_open_db`、马丁加仓等同前

### 核心服务
- **`binance_service.py`** / **`gate_service.py`**：各自 ccxt 封装与 `fetch_top_movers`（Gate 用官方 `change_percentage`，勿用 ccxt.percentage）。Gate 开仓按 `quanto_multiplier` 换算张数；`skip_min_qty_exceeds`（默认开）低于交易所最小名义则软跳过
- **`position_manager.py`**：`manage_symbol` / `evaluate_signal` / 开仓两阶段；止盈限价挂撤与成交写库；单币止损文案为「保证金余额」
- **`scheduler.py`**：保证金阈值止损比较 `extract_margin_balance`；变量名 `public_binance`/`auth_binance` 可能实际是 Gate 客户端
- **`coin_pool_service.py`**：按 **`exchange`** 隔离刷新/查询/配置/状态。`config_for(ex)`、`status_for(ex)`；`refresh_pool` 校验 `client.exchange_id` 与参数一致。自动循环对运行中账户涉及的每个所分别 `get_public_exchange` + 刷新
- **`equity_snapshot_job.py`**：整点快照写入 **保证金余额**（非钱包余额）；币安另同步划转流水
- **`equity` 路由**：重置收益清空快照 → 写入当前基准 → **立即写入一条当前余额快照**（避免无点位时显示 -100%）；无快照时 summary 盈亏/回报归零。曲线盈亏/收益率按**所选窗口起点**算，不是相对全局 baseline。`normalize_baseline_to_gross` 自动修复异常偏小/负数基准（持久化回库）
- **`kline_stream.py`**：key 含 `exchange_id`；`peek` 热路径零 await；`refresh_forming` 武装/反弹窗轻量 REST 补本根（本根 H/L/V **只增不减**）；**已收盘 K 全量采用 REST**（可降可升，勿把盘中毛刺锁进 ATR）
- **`price_stream.py`**：币安 `watch_trades`；本根 `bar_volume`/`bar_high`/`bar_low`；成交唤醒 Event；断连重连后 `take_resync_needed(本策略池)` 强制补 forming
- **`account_position_stream.py`**：User Data Stream 腿缓存；热路径优先于 REST `fetch_positions`；平仓 `apply_local_close(qty)` 按量扣减；空心跳只续期不清全表
- **`wick_spike_engine.py`**：Arm–Confirm 纯逻辑；**`wick_spike_runner.py`**：每策略价流循环（市价开仓后台化，不堵扫描）
- **`trailing_tp_engine.py`**：时间移动止盈纯逻辑；runner 毫秒追踪；持仓页 `/api/strategies/{id}/trailing-status`
- 其余：`sync_service`、`strategy_engine`、`martingale_engine`、`websocket_manager`、`backup_service` 等

### 毫秒接针（`wick_spike`，仅币安）

**状态机（Arm–Confirm）**（`wick_spike_engine.on_tick`）：
1. 刺破（极值相对开盘 ≥ ATR×`atr_mult`）+ 最小涨跌幅 `min_move_pct` → **武装**
2. 武装窗内等量能达标 → 确认开仓
3. 武装时量不够则 `awaiting_vol`：grace 秒内免回撤，但受 `arm_grace_max_tip_gap_pct`（默认 2%）上限
4. 超时作废后：**同根仍刺破即可再武装**（不要求 progress 创新高）
5. `progress` 量能放宽：progress≥1.5 时 need 降到 `vol_relax_mult`（默认 5×）

**武装窗 `arm_wait_sec`（产品默认 0，方案 B）**：
- `>0`：本根内 N 秒超时
- `0`：本根内不超时，**换根才超时**（与反弹一致）
- `-1`：关闭武装，恢复同刻全条件

**反弹追踪**（产品新建默认开）：confirm 后进窗等反弹达 `trigger%` 再市价；`abort%` 放弃。`rebound_wait=0`：本根内不按秒超时；**换根时若仍在反弹窗 → timeout 并锁住新根**（禁止同一次插针跨分钟立刻再开火，AIO 案例）。

**成交量确认 `wick_volume_mode`**：
- `original`：瞬时折算量全程纳入（旧行为）
- `instant_early`：本根前段用瞬时量，进度超过 `instant_active_until_pct`（默认 50%）后改用真实累计量
- `real_only`：只用 K 线/成交累计真实量

**数据与热路径**：
- ATR / 量均只用**已收盘 K**；`atrN = ATR × atr_mult`
- `enrich_snap_with_trades`：高低/累计 vol 对齐本根才并入；瞬时量是否纳入看 `volume_mode`
- 武装等量 **或** 反弹窗内：约 2s 后台 `refresh_forming`（最近 2 根）
- forming 停滞：后台 REST 纠偏；成交流已在新根则 `merge_synthetic_forming_bar`（开盘优先用本根第一笔成交价）继续 `on_tick`，勿整币跳过
- 市价 `_try_open` **后台化**（`open_inflight` 防双开），不阻塞同策略其它币扫描；TP/DB 仍 fire-and-forget
- 开仓门禁：新鲜账户流优先；流空/缺失须 REST 复核（勿用过期 `tick_ctx` 假 `has_pos` 锁根）
- **热路径禁止 await REST**；参数/选币池后台槽消费

**多账户 ATR**：同进程内 `kline_stream` 按 `(exchange, symbol, tf)` 共享缓冲。`atrN` 偏大 → progress 偏小 → need× 放宽不够 → 易近失。

**策略只管理机器人仓**：默认不领养孤儿；止盈/平仓按机器人数量 `close_position_qty`；同向已有交易所腿则跳过新开（不叠手动仓）。账户删除仍全腿平仓。

### 时间移动止盈（`trailing_tp`，仅 `wick_spike`）

开关 `trailing_tp_enabled`（默认关）。开仓后 `armed`：窗口内（默认 300s）盈利达 `take_profit_pct` → `active` 毫秒追踪峰值回撤（阶梯：默认 <2.5% 用 30%、≥2.5% 用 20%、≥5% 用 15%）；窗口超时 → `expired` 回退限价止盈。马丁加仓后 trailing 置 `expired`，整腿改限价。`manage_symbol` 在 armed/active 时不挂限价、不市价兜底止盈。开仓后立即写入 runner 内存态（勿等 15s refresh）。持仓页轮询 `GET /api/strategies/{id}/trailing-status`。

### 限价止盈与市价兜底（易踩坑）

**挂单**：`get_take_profit_price` = 入场×(1±`take_profit_pct`/100)；开仓/马丁撤旧挂新；币安绑单按 `positionSide`+平仓方向+数量（**不强制** reduceOnly）。

**有挂单且状态仍 open/new/partial**：**只等限价成交**，禁止「最新价已过止盈价」市价穿越兜底（接针后价格常瞬穿又弹回，市价会在更差价位平掉——BICO/HEI 案例）。

**市价兜底仅当**：未开限价止盈 / 无 `tp_limit_order_id` / 限价已取消或过期。

**触发判定（将走市价时）**：必须用 `fetch_ticker` 最新价；ticker 失败则不软触发。禁止用滞后 forming K 线 close（接针后 close 常停在开盘附近）。

**出场价写库**（`_order_fill_avg_price` / `_resolve_market_close_exit`）：
1. `average` / `info.avgPrice` / `cost÷filled` / `cumQuote`
2. 重查订单；市价再按 `orderId` `fetch_my_trades` 算 VWAP
3. 仍无则 ticker last；再无则**不写库**
4. **禁止**用限价挂单价 `price` 冒充成交均价（`allow_order_price=False`）——曾误把理论止盈价写成出场导致盈亏夸大

本地 `realized_pnl` = 价差×数量，**不含手续费**，与币安「已实现盈亏」会有差额。

### API 路由总览

| 路由模块 | 前缀 | 关键端点 |
|---|---|---|
| `routers/account.py` | `/api/accounts` | CRUD（含 `exchange`），删除三阶段清理 |
| `routers/strategies.py` | `/api/strategies` | CRUD + start/stop/panic-close + 有效选币池 + `trailing-status` |
| `routers/positions.py` | `/api/positions` | 列表/手动平仓/撤销止盈单 |
| `routers/trades.py` | `/api/trades` | 列表/删除/备份统计/恢复/CSV导出 |
| `routers/dashboard.py` | `/api/dashboard` | 返回 `wallet_balance` + `margin_balance`（`total_balance`=保证金，兼容旧字段） |
| `routers/coin_pool.py` | `/api/coin-pool` | 列表/刷新/配置/测试/status，均支持 `?exchange=` |
| `routers/equity.py` | `/api/equity` | 权益曲线 / 重置基准 |
| `routers/websocket.py` | `/ws/market`, `/ws/dashboard` | 行情推送（**market 目前固定币安**）+ 仪表盘 |
| `main.py` 内联 | `/api/health`, `/api/klines`, `/api/ticker`, … | K线/Ticker **目前固定 `get_public_binance()`**（图表分析页） |

### 历史交易备份与 REST（`/api/trades`）
- **备份写入**：须在 `Trade` **持久化拿到 `id` 之后**再 `backup_trade()`
- **按账户隔离**：备份统计/恢复/批量删除均带 `account_id`
- **路由顺序**：`DELETE ""` 必须在 `DELETE /{trade_id}` 之前

### 数据库
- SQLite + aiosqlite；`PRAGMA foreign_keys=ON`
- **CoinPool**：`UniqueConstraint("exchange", "symbol", "source")`；涨/跌榜与币安/GATE 互不覆盖
- 模型含 Account（`exchange`）、Strategy、Position、Trade、BotConfig、AccountBalanceSnapshot、AccountEquityBaseline、AccountCashflow、StrategySymbolBlacklist 等
- 时间均为无时区北京时间（`now_beijing()`）

### 账户删除
三阶段：停策略 → 平该账户交易所仓位 → 显式清关联表（含权益/现金流等），CASCADE 兜底

### Dashboard
- 卡片：**钱包余额** | **保证金余额** | 杠杆 | 未实现盈亏 | 当前持仓（已去掉「活跃策略」）
- 顶栏「余额」用保证金余额（随浮盈亏变）
- 60s TTL 按 `account_id` 缓存；跟顶栏选中账户走对应交易所 API
- 「策略收益」：快照=保证金余额；**重启引导快照**会在非整点多一个点（设计如此，非 bug）

### 选币池（交易所隔离）
- 写库/删库/查询一律带 `CoinPool.exchange`
- **策略 tick / 有效池**：跟策略账户的 `exchange`
- **选币池页面**：自带「币安 / GATE」开关，**不自动跟顶栏账户联动**；配置 GET/PUT、刷新、test-fetch 均传 `exchange`
- **间隔选项**：10/15/30 分钟，1/2/4/8/12/24 小时（秒：600…86400）；策略表单与选币池页共用 `COIN_POOL_REFRESH_OPTIONS`
- **迁移注意**：`migrate_coin_pool_symbol_source_unique` 若表已有 `exchange` 列必须跳过，否则会拆列并把 GATE 行标成 binance（已修）

### 数据库迁移
- **内联**（`init_db()`）：大量 ALTER + NULL 回填；`accounts.exchange` 空值回填 `binance`（不碰已有 `gate`）
- **文件**：`db_migrations/coin_pool_unique.py`（仅无 exchange 的旧表）、`coin_pool_exchange.py` → `(exchange, symbol, source)`
- **Alembic**：`deploy.sh` 每次 `upgrade head`；无新 revision 时为空操作

### 前端
- React 19 + TypeScript + Vite 6 + TailwindCSS 4 + Zustand 5 + React Router 7 + lightweight-charts 4
- **认证**：owner / guest；密码 `sessionStorage`
- **路由**：`/` 仪表盘、`/chart` 图表分析（**行情固定币安**）、策略、持仓、交易、选币池、设置、接针统计/查币种监控
- 生产：仅需后端 8000 托管 `dist/`；勿依赖 deploy 拉起的 vite preview
- 顶栏 `selectedAccountId` 与 `dashboardStore` 同步；仪表盘/持仓/交易历史跟账户；选币池页独立交易所开关
- 仅改后端服务逻辑时**不必**为前端单独发版（`deploy.sh` 仍会 build，产物可不变）

### 测试
含选币池交易所隔离、余额提取、接针引擎/日志/监控、Gate、调度、止盈取价、移动止盈、账户流清腿等。运行：`cd backend && python -m pytest tests/ -v`。接针相关：`tests/test_wick_spike_*.py`、`test_wick_symbol_monitor.py`、`test_trailing_tp_engine.py`。

### 重启影响
- 资金/持仓/限价单在交易所；`resume_running_strategies` 从 DB 恢复
- 接针内存武装态丢失；重启窗口可能错过深针
- 权益曲线多一个引导快照点
- 建议避开整点与 K 线收盘附近重启（`:05`–`:50` 较稳）

## 关键约定

- **多交易所**：交易执行、余额、选币池（调度侧）一律按**账户 `exchange`**；禁止用 `max(钱包, 保证金)` 取余额（浮亏会冻在钱包上）
- **余额口径**：App 钱包=`totalWalletBalance`；App 保证金/`totalMarginBalance` 用于曲线、保证金止损、单币止损、顶栏
- **双向持仓**：`positionSide` + 平仓 `reduceOnly`（单向账户不发）
- **限价止盈**：有挂单 open → **只等限价**；市价兜底仅无限价/已取消；触发与写库均禁止用滞后 K 线价或挂单价冒充成交价（见上文「限价止盈与市价兜底」）
- **接针**：热路径不 await REST；市价开仓后台化，`_try_open` 异常不得打崩整条 runner；触发日志勿引用循环局部变量；反弹/武装 `wait=0` 换根才超时并锁新根；多策略 resync 用 `take_resync_needed(本池)` 勿全局 consume
- **仓位隔离**：策略只平/管机器人仓；平仓立刻 `apply_local_close`；UDS 空包不清全表，但平仓后不得残留腿永久屏蔽该币
- **移动止盈**：仅接针；armed/active 禁止限价穿越市价兜底；加仓后 expired 改限价
- **符号标准化**：去 `/`、`:USDT`、大写
- **TradFi / 下架 / 主流 / 费率**：策略级开关；已有持仓不因过滤抛弃
- **新增 DB 列**：model + schema + 前端 types + `init_db` 迁移 + NULL 兜底
- **Python**：本机 `python`；服务器 `python3` + `backend/.venv/`
- **进程**：生产用进程守护重启后端；`deploy.sh` 不启停进程
- **Git 身份**：本仓库 `user.name "ryf13719652107"` / `user.email "ryf13719652107@gmail.com"`，勿改全局
- **部署教程**：`部署教程.md`（若本地 ignore，以仓库/服务器文档为准）
