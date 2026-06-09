import logging
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
import os

from .config import settings

logger = logging.getLogger(__name__)

# Ensure data directory exists
db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
db_dir = os.path.dirname(db_path)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    # WAL + busy_timeout: background sync now runs detached from the tick and may
    # overlap the next tick's writes on a separate connection. WAL lets readers and
    # one writer coexist; busy_timeout makes a competing writer wait instead of
    # immediately raising "database is locked".
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    from .models.equity_curve import AccountBalanceSnapshot, AccountEquityBaseline  # noqa: F401
    from .models.strategy_blacklist import StrategySymbolBlacklist  # noqa: F401

    # Create tables from current model (no-op if already exist)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)
        # Add new columns that may not exist in existing databases
        migrations = [
            "ALTER TABLE strategies ADD COLUMN wt_ob_level FLOAT DEFAULT 60.0",
            "ALTER TABLE strategies ADD COLUMN wt_os_level FLOAT DEFAULT -60.0",
            "ALTER TABLE positions ADD COLUMN opened_at TIMESTAMP",
            "ALTER TABLE positions ADD COLUMN closed_at TIMESTAMP",
            "ALTER TABLE strategies ADD COLUMN exclude_tradefi BOOLEAN DEFAULT 1",
            "ALTER TABLE strategies ADD COLUMN exclude_delisting BOOLEAN DEFAULT 1",
            "ALTER TABLE strategies ADD COLUMN exclude_mainstream BOOLEAN DEFAULT 1",
            "ALTER TABLE strategies ADD COLUMN exclude_funding BOOLEAN DEFAULT 0",
            "ALTER TABLE strategies ADD COLUMN funding_rate_threshold_pct FLOAT DEFAULT 0",
            "ALTER TABLE strategies ADD COLUMN coin_pool_min_volume_24h FLOAT DEFAULT 0",
            "ALTER TABLE strategies ADD COLUMN price_drop_multiplier FLOAT DEFAULT 1.0",
            "ALTER TABLE strategies ADD COLUMN coin_pool_anchor_hour INTEGER DEFAULT 8",
            "ALTER TABLE strategies ADD COLUMN coin_pool_anchor_minute INTEGER DEFAULT 0",
            "ALTER TABLE strategies ADD COLUMN coin_pool_schedule_started_at TIMESTAMP",
        ]
        for sql in migrations:
            try:
                await conn.run_sync(lambda c, s=sql: c.exec_driver_sql(s))
            except Exception:
                pass  # column already exists

        # Backfill NULL opened_at for existing positions
        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE positions SET opened_at = datetime('now', 'localtime') WHERE opened_at IS NULL"
                )
            )
        except Exception:
            pass

        from .db_migrations.coin_pool_unique import migrate_coin_pool_symbol_source_unique

        try:
            await conn.run_sync(migrate_coin_pool_symbol_source_unique)
        except Exception as e:
            logger.warning("coin_pool (symbol, source) migration skipped or failed: %s", e)

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET exclude_delisting=1 "
                    "WHERE exclude_delisting IS NULL"
                )
            )
        except Exception:
            pass

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET exclude_mainstream=1 "
                    "WHERE exclude_mainstream IS NULL"
                )
            )
        except Exception:
            pass

        # Backfill NULL coin_pool_anchor_minute for existing strategies
        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET coin_pool_anchor_minute=0 "
                    "WHERE coin_pool_anchor_minute IS NULL"
                )
            )
        except Exception:
            pass

        # Backfill NULL price_drop_multiplier for existing strategies
        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET price_drop_multiplier=1.0 "
                    "WHERE price_drop_multiplier IS NULL"
                )
            )
        except Exception:
            pass



