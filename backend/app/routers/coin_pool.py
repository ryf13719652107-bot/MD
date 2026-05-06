from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas.coin_pool import CoinPoolConfig, CoinPoolResponse
from ..services.coin_pool_service import coin_pool_service

router = APIRouter(prefix="/api/coin-pool", tags=["coin_pool"])


@router.get("", response_model=list[CoinPoolResponse])
async def get_coin_pool(source: str | None = None):
    coins = await coin_pool_service.get_pool(source)
    return [CoinPoolResponse.model_validate(c) for c in coins]


@router.post("/refresh")
async def refresh_coin_pool():
    """Manually refresh the coin pool. Returns success/failure status, never 500."""
    from ..services.binance_service import get_public_binance

    try:
        binance = get_public_binance()
        await coin_pool_service.refresh_pool(binance)
        return {"status": "ok", "message": "选币池刷新成功"}
    except Exception as e:
        return {"status": "error", "message": f"刷新失败: {str(e)}"}


@router.get("/config", response_model=CoinPoolConfig)
async def get_coin_pool_config():
    return CoinPoolConfig(**coin_pool_service.config)


@router.put("/config", response_model=CoinPoolConfig)
async def update_coin_pool_config(data: CoinPoolConfig):
    coin_pool_service.update_config(**data.model_dump())
    return CoinPoolConfig(**coin_pool_service.config)


@router.post("/test-fetch")
async def test_fetch_coin_pool():
    """Test fetching top movers from Binance without saving to DB. Returns raw results and any errors."""
    from ..services.binance_service import get_public_binance

    try:
        binance = get_public_binance()
        movers = await binance.fetch_top_movers(source="both", limit=20)
        return {
            "success": True,
            "count": len(movers),
            "data": movers[:5] if movers else [],
            "message": f"成功获取 {len(movers)} 个交易对" if movers else "未获取到数据，请检查网络或币安API状态",
        }
    except Exception as e:
        return {
            "success": False,
            "count": 0,
            "data": [],
            "message": f"获取失败: {str(e)}",
        }


@router.get("/status")
async def coin_pool_status():
    """Get coin pool diagnostic status."""
    count = await coin_pool_service.get_pool_count()
    status = coin_pool_service.status
    return {
        "total_symbols": count,
        "last_refresh_ok": status["last_refresh_ok"],
        "last_refresh_time": status["last_refresh_time"],
        "last_error": status["last_error"],
        "config": coin_pool_service.config,
    }
