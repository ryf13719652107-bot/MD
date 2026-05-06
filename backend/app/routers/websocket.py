import asyncio
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from ..services.websocket_manager import ws_manager
from ..services.binance_service import get_public_binance

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])


@router.websocket("/ws/market")
async def market_websocket(websocket: WebSocket, symbols: str = Query(default="")):
    await ws_manager.connect(websocket, "market")
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []

    try:
        if symbol_list:
            binance = get_public_binance()
            while True:
                try:
                    tickers = await binance.watch_tickers(symbol_list)
                    if isinstance(tickers, dict):
                        for sym, ticker in tickers.items():
                            if isinstance(ticker, dict):
                                clean_sym = sym.replace("/", "").replace(":USDT", "")
                                await ws_manager.broadcast(
                                    "market",
                                    {
                                        "type": "ticker",
                                        "symbol": clean_sym,
                                        "price": ticker.get("last"),
                                        "change_24h": ticker.get("percentage"),
                                        "volume": ticker.get("quoteVolume"),
                                        "timestamp": ticker.get("timestamp"),
                                    },
                                )
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error("Market WS error: %s", e)
                    await asyncio.sleep(2)
        else:
            while True:
                await asyncio.sleep(1)
                await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket, "market")


@router.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    await ws_manager.connect(websocket, "dashboard")
    try:
        while True:
            await asyncio.sleep(3)
            await ws_manager.broadcast(
                "dashboard",
                {
                    "type": "snapshot",
                    "timestamp": int(asyncio.get_event_loop().time() * 1000),
                    "message": "request_update",  # Frontend should re-fetch /api/dashboard
                },
            )
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket, "dashboard")
