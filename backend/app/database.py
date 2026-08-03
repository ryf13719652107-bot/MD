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

# aiosqlite timeout：等锁秒数（与 PRAGMA busy_timeout 配合）
engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args={"timeout": 30},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    # WAL + busy_timeout: 整点权益快照 / 后台 sync / 多策略 tick 可能重叠写库。
    # busy_timeout 单位 ms；过短时整点易报 database is locked。
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
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
    from .models.equity_curve import AccountBalanceSnapshot, AccountEquityBaseline, AccountCashflow  # noqa: F401
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
            "ALTER TABLE strategies ADD COLUMN single_symbol_stop_loss_enabled BOOLEAN DEFAULT 0",
            "ALTER TABLE strategies ADD COLUMN single_symbol_stop_loss_pct FLOAT DEFAULT 10",
            "ALTER TABLE strategies ADD COLUMN st_atr_period INTEGER DEFAULT 10",
            "ALTER TABLE strategies ADD COLUMN st_factor FLOAT DEFAULT 3.0",
            "ALTER TABLE strategies ADD COLUMN st_timeframe_1 VARCHAR(10) DEFAULT '15m'",
            "ALTER TABLE strategies ADD COLUMN st_timeframe_2 VARCHAR(10) DEFAULT '30m'",
            "ALTER TABLE strategies ADD COLUMN martingale_st_filter_enabled BOOLEAN DEFAULT 0",
            "ALTER TABLE strategies ADD COLUMN wick_volume_mult FLOAT DEFAULT 8.0",
            "ALTER TABLE strategies ADD COLUMN wick_volume_sma_period INTEGER DEFAULT 20",
            "ALTER TABLE strategies ADD COLUMN wick_atr_period INTEGER DEFAULT 14",
            "ALTER TABLE strategies ADD COLUMN wick_spike_atr_mult FLOAT DEFAULT 5.0",
            "ALTER TABLE strategies ADD COLUMN wick_cooldown_sec INTEGER DEFAULT 0",
            "ALTER TABLE strategies ADD COLUMN wick_amp_vol_relax_enabled BOOLEAN DEFAULT 1",
            "ALTER TABLE strategies ADD COLUMN wick_vol_relax_progress_start FLOAT DEFAULT 1.0",
            "ALTER TABLE strategies ADD COLUMN wick_vol_relax_progress_full FLOAT DEFAULT 1.5",
            "ALTER TABLE strategies ADD COLUMN wick_vol_relax_mult FLOAT DEFAULT 5.0",
            "ALTER TABLE accounts ADD COLUMN cashflow_sync_cursor_ms INTEGER",
            "ALTER TABLE accounts ADD COLUMN exchange VARCHAR(20) DEFAULT 'binance'",
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

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE accounts SET exchange='binance' WHERE exchange IS NULL OR exchange=''"
                )
            )
        except Exception:
            pass

        from .db_migrations.coin_pool_unique import migrate_coin_pool_symbol_source_unique
        from .db_migrations.coin_pool_exchange import migrate_coin_pool_exchange_unique

        try:
            await conn.run_sync(migrate_coin_pool_symbol_source_unique)
        except Exception as e:
            logger.warning("coin_pool (symbol, source) migration skipped or failed: %s", e)

        try:
            await conn.run_sync(migrate_coin_pool_exchange_unique)
        except Exception as e:
            logger.warning("coin_pool (exchange, symbol, source) migration skipped or failed: %s", e)

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE coin_pool SET exchange='binance' "
                    "WHERE exchange IS NULL OR exchange=''"
                )
            )
        except Exception:
            pass

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

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET wick_amp_vol_relax_enabled=1 "
                    "WHERE wick_amp_vol_relax_enabled IS NULL"
                )
            )
        except Exception:
            pass

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET wick_vol_relax_progress_start=1.0 "
                    "WHERE wick_vol_relax_progress_start IS NULL"
                )
            )
        except Exception:
            pass

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET wick_vol_relax_progress_full=1.5 "
                    "WHERE wick_vol_relax_progress_full IS NULL"
                )
            )
        except Exception:
            pass

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET wick_vol_relax_mult=5.0 "
                    "WHERE wick_vol_relax_mult IS NULL"
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

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET single_symbol_stop_loss_enabled=0 "
                    "WHERE single_symbol_stop_loss_enabled IS NULL"
                )
            )
        except Exception:
            pass

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET single_symbol_stop_loss_pct=10 "
                    "WHERE single_symbol_stop_loss_pct IS NULL"
                )
            )
        except Exception:
            pass

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET st_timeframe_1='15m' "
                    "WHERE st_timeframe_1 IS NULL OR st_timeframe_1=''"
                )
            )
        except Exception:
            pass

        try:
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    "UPDATE strategies SET st_timeframe_2='30m' "
                    "WHERE st_timeframe_2 IS NULL OR st_timeframe_2=''"
                )
            )
        except Exception:
            pass



