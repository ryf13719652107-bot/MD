from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas.coin_pool import CoinPoolConfig, CoinPoolResponse
from ..services.coin_pool_service import coin_pool_service
from ..services.exchange_factory import get_public_exchange, normalize_exchange_id

router = APIRouter(prefix="/api/coin-pool", tags=["coin_pool"])


@router.get("", response_model=list[CoinPoolResponse])
async def get_coin_pool(
    source: str | None = None,
    strategy_id: int | None = None,
    exchange: str = Query(default="binance"),
    db: AsyncSession = Depends(get_db),
):
    from ..services.coin_pool_presenter import coin_pool_responses_with_funding
    from ..models.strategy import Strategy
    from ..models.account import Account

    ex = normalize_exchange_id(exchange)
    if strategy_id is not None:
        strategy = await db.get(Strategy, strategy_id)
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")
        acc = await db.get(Account, strategy.account_id)
        ex = normalize_exchange_id(getattr(acc, "exchange", None) if acc else ex)
        coins = await coin_pool_service.get_pool_for_strategy(
            source=source, strategy=strategy, exchange=ex
        )
    else:
        coins = await coin_pool_service.get_pool(source, exchange=ex)
    public = await get_public_exchange(ex)
    return await coin_pool_responses_with_funding(public, coins)


@router.post("/refresh")
async def refresh_coin_pool(exchange: str = Query(default="binance")):
    """Manually refresh the coin pool. Returns success/failure status, never 500."""
    ex = normalize_exchange_id(exchange)
    if await coin_pool_service.has_running_scheduled_strategies(exchange=ex):
        return {
            "status": "error",
            "message": f"该交易所({ex})存在「指定时间开选」的运行中策略，请等待计划时刻自动选币",
        }

    try:
        client = await get_public_exchange(ex)
        await coin_pool_service.refresh_pool_sources(client, exchange=ex)
        return {"status": "ok", "message": f"选币池刷新成功 ({ex})"}
    except Exception as e:
        return {"status": "error", "message": f"刷新失败: {str(e)}"}


@router.get("/config", response_model=CoinPoolConfig)
async def get_coin_pool_config(exchange: str = Query(default="binance")):
    ex = normalize_exchange_id(exchange)
    return CoinPoolConfig(**coin_pool_service.config_for(ex))


@router.put("/config", response_model=CoinPoolConfig)
async def update_coin_pool_config(
    data: CoinPoolConfig,
    exchange: str = Query(default="binance"),
):
    ex = normalize_exchange_id(exchange)
    coin_pool_service.update_config(exchange=ex, **data.model_dump())
    return CoinPoolConfig(**coin_pool_service.config_for(ex))


@router.post("/test-fetch")
async def test_fetch_coin_pool(exchange: str = Query(default="binance")):
    """Test fetching top movers without saving to DB."""
    ex = normalize_exchange_id(exchange)
    try:
        client = await get_public_exchange(ex)
        src = coin_pool_service.config_for(ex).get("pool_source", "both")
        movers = await client.fetch_top_movers(source=src, limit=20)
        return {
            "success": True,
            "count": len(movers),
            "data": movers[:5] if movers else [],
            "exchange": ex,
            "message": (
                f"成功获取 {len(movers)} 个交易对 ({ex})"
                if movers
                else f"未获取到数据，请检查网络或 {ex} API 状态"
            ),
        }
    except Exception as e:
        return {
            "success": False,
            "count": 0,
            "data": [],
            "exchange": ex,
            "message": f"获取失败: {str(e)}",
        }


@router.get("/status")
async def coin_pool_status(exchange: str = Query(default="binance")):
    """Get coin pool diagnostic status (per exchange)."""
    ex = normalize_exchange_id(exchange)
    count = await coin_pool_service.get_pool_count(exchange=ex)
    status = coin_pool_service.status_for(ex)
    return {
        "total_symbols": count,
        "exchange": ex,
        "last_refresh_ok": status["last_refresh_ok"],
        "last_refresh_time": status["last_refresh_time"],
        "last_error": status["last_error"],
        "config": coin_pool_service.config_for(ex),
    }
