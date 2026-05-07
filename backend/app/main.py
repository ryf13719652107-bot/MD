import logging
import os
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from .config import settings
from .database import init_db, get_db, async_session
from .models.bot_config import BotConfig
from .models.strategy import Strategy
from .routers import account, strategies, positions, trades, dashboard, coin_pool, websocket
from .services.scheduler import strategy_scheduler
from .services.binance_service import get_public_binance
from .services.coin_pool_service import coin_pool_service

# ---- Logging Setup ----
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# File handler with rotation (10MB x 5 files)
file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "bot.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
))

# Root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Quiet noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("Smart Hedge Martin starting...")
    logger.info("Step 1/6: init_db...")
    await init_db()
    logger.info("Step 2/6: reset_stale...")
    await strategy_scheduler._reset_stale_running_strategies()
    logger.info("Step 3/6: scheduler.start...")
    strategy_scheduler.start()
    logger.info("Step 4/6: get_public_binance...")
    public_binance = await get_public_binance()

    # Apply coin pool config from strategies
    logger.info("Step 5/6: coin pool config...")
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(Strategy).where(Strategy.use_coin_pool == True, Strategy.status == "running").order_by(Strategy.coin_pool_refresh_seconds).limit(1)
        )
        strategy_with_pool = result.scalar()
        if strategy_with_pool:
            coin_pool_service.update_config(
                refresh_interval_seconds=strategy_with_pool.coin_pool_refresh_seconds,
                pool_source=strategy_with_pool.coin_pool_source,
            )

    logger.info("Step 6/6: start_auto_refresh...")
    await coin_pool_service.start_auto_refresh(public_binance)
    logger.info("Coin pool auto-refresh started")
    logger.info("Backend ready")
    yield
    logger.info("Shutting down...")
    strategy_scheduler.stop()
    await coin_pool_service.stop_auto_refresh()
    logger.info("Backend stopped")


import os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(
    title="Smart Hedge Martin",
    description="智能对冲交易机器人系统",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import logging
    logger = logging.getLogger(__name__)
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误"},
    )


# Register routers
app.include_router(account.router)
app.include_router(strategies.router)
app.include_router(positions.router)
app.include_router(trades.router)
app.include_router(dashboard.router)
app.include_router(coin_pool.router)
app.include_router(websocket.router)

# Serve frontend static files
frontend_dist = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.isdir(frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dist, "assets")), name="static")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        """SPA fallback — serve index.html for non-API routes."""
        file_path = os.path.join(frontend_dist, full_path) if full_path else ""
        if full_path and os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(frontend_dist, "index.html"))


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/klines")
async def get_klines(
    symbol: str = Query(...),
    timeframe: str = Query(default="1m"),
    limit: int = Query(default=200, le=500),
):
    binance = await get_public_binance()
    klines = await binance.fetch_klines(symbol, timeframe, limit)
    return [
        {
            "time": k[0],
            "open": k[1],
            "high": k[2],
            "low": k[3],
            "close": k[4],
            "volume": k[5],
        }
        for k in klines
    ]


@app.get("/api/ticker")
async def get_ticker(symbol: str = Query(...)):
    binance = await get_public_binance()
    ticker = await binance.fetch_ticker(symbol)
    return {
        "symbol": symbol,
        "last": ticker.get("last"),
        "change_pct": ticker.get("percentage"),
        "high_24h": ticker.get("high"),
        "low_24h": ticker.get("low"),
        "volume_24h": ticker.get("quoteVolume"),
    }


class ToggleRequest(BaseModel):
    enabled: bool


@app.get("/api/logs")
async def view_logs(lines: int = Query(default=100, le=1000)):
    """Return the last N lines of the log file."""
    log_file = os.path.join(LOG_DIR, "bot.log")
    if not os.path.exists(log_file):
        return {"lines": [], "message": "日志文件不存在"}
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return {"lines": [l.rstrip() for l in recent], "total": len(all_lines)}
    except Exception as e:
        return {"lines": [], "message": str(e)}


@app.post("/api/bot/toggle")
async def toggle_bot(body: ToggleRequest, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    enabled = body.enabled
    result = await db.execute(select(BotConfig).where(BotConfig.key == "master_switch"))
    config = result.scalar()
    if config:
        config.value = "true" if enabled else "false"
    else:
        config = BotConfig(key="master_switch", value="true" if enabled else "false")
        db.add(config)
    await db.commit()
    return {"master_switch": enabled}
