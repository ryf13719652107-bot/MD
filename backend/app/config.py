from pydantic_settings import BaseSettings
from typing import Optional
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    """Return current Beijing time as naive datetime (for SQLite compatibility)."""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///data/trading_bot.db"
    encryption_key: Optional[str] = None
    binance_api_key: Optional[str] = None
    binance_secret: Optional[str] = None
    binance_testnet: bool = True
    cors_origins: str = "http://localhost:5173"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
