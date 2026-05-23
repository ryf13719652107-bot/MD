"""选币池：同一 symbol 可存在于不同 source。"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.coin_pool import CoinPool
from app.db_migrations.coin_pool_unique import migrate_coin_pool_symbol_source_unique


def test_same_symbol_different_sources():
    engine = create_engine("sqlite:///:memory:")
    CoinPool.__table__.create(engine)
    with Session(engine) as session:
        session.add(
            CoinPool(
                symbol="WIFUSDT",
                rank=1,
                price_change_pct=5.0,
                volume_24h=1e8,
                source="gainers",
            )
        )
        session.add(
            CoinPool(
                symbol="WIFUSDT",
                rank=1,
                price_change_pct=2.0,
                volume_24h=1e8,
                source="losers",
            )
        )
        session.commit()
        rows = session.execute(select(CoinPool)).scalars().all()
    assert len(rows) == 2
    assert {r.source for r in rows} == {"gainers", "losers"}


def test_migrate_removes_symbol_only_unique():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE coin_pool (
                id INTEGER PRIMARY KEY,
                symbol VARCHAR(50) NOT NULL UNIQUE,
                rank INTEGER NOT NULL,
                price_change_pct FLOAT NOT NULL,
                volume_24h FLOAT,
                source VARCHAR(20) NOT NULL,
                added_at DATETIME,
                last_updated DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO coin_pool (symbol, rank, price_change_pct, volume_24h, source) "
            "VALUES ('X', 1, 1.0, 1e9, 'gainers')"
        )
        migrate_coin_pool_symbol_source_unique(conn)
        conn.exec_driver_sql(
            "INSERT INTO coin_pool (symbol, rank, price_change_pct, volume_24h, source) "
            "VALUES ('X', 1, 2.0, 1e9, 'losers')"
        )
