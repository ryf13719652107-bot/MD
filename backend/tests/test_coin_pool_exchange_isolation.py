"""选币池按交易所隔离：旧迁移不得拆掉 exchange 列。"""

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.db_migrations.coin_pool_exchange import migrate_coin_pool_exchange_unique
from app.db_migrations.coin_pool_unique import migrate_coin_pool_symbol_source_unique
from app.models.coin_pool import CoinPool


def _create_triple_table(conn) -> None:
    conn.exec_driver_sql(
        """
        CREATE TABLE coin_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange VARCHAR(20) NOT NULL DEFAULT 'binance',
            symbol VARCHAR(50) NOT NULL,
            rank INTEGER NOT NULL,
            price_change_pct FLOAT NOT NULL,
            volume_24h FLOAT,
            source VARCHAR(20) NOT NULL,
            added_at DATETIME,
            last_updated DATETIME,
            CONSTRAINT uq_coinpool_exchange_symbol_source
                UNIQUE (exchange, symbol, source)
        )
        """
    )


def test_old_migration_skips_when_exchange_column_exists():
    """回归：已有 exchange 的表再跑旧迁移，不得拆列/合并所。"""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_triple_table(conn)
        conn.exec_driver_sql(
            "INSERT INTO coin_pool "
            "(exchange, symbol, rank, price_change_pct, volume_24h, source) "
            "VALUES ('binance', 'BTCUSDT', 1, 1.0, 1e9, 'gainers')"
        )
        conn.exec_driver_sql(
            "INSERT INTO coin_pool "
            "(exchange, symbol, rank, price_change_pct, volume_24h, source) "
            "VALUES ('gate', 'BLESSUSDT', 1, 69.0, 3e7, 'gainers')"
        )
        migrate_coin_pool_symbol_source_unique(conn)
        migrate_coin_pool_exchange_unique(conn)

        cols = {
            r[1]
            for r in conn.execute(text("PRAGMA table_info(coin_pool)")).fetchall()
        }
        assert "exchange" in cols
        rows = conn.execute(
            text("SELECT exchange, symbol FROM coin_pool ORDER BY exchange")
        ).fetchall()
        assert rows == [("binance", "BTCUSDT"), ("gate", "BLESSUSDT")]


def test_init_migration_sequence_preserves_both_exchanges():
    """模拟 init_db：旧迁移 + 新迁移连续执行后双所数据仍在。"""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_triple_table(conn)
        conn.exec_driver_sql(
            "INSERT INTO coin_pool "
            "(exchange, symbol, rank, price_change_pct, volume_24h, source) "
            "VALUES ('binance', 'HOMEUSDT', 2, 35.0, 2.5e8, 'gainers')"
        )
        conn.exec_driver_sql(
            "INSERT INTO coin_pool "
            "(exchange, symbol, rank, price_change_pct, volume_24h, source) "
            "VALUES ('gate', 'HOMEUSDT', 2, 36.0, 3.9e7, 'gainers')"
        )
        # 连续跑两次（重启场景）
        for _ in range(2):
            migrate_coin_pool_symbol_source_unique(conn)
            migrate_coin_pool_exchange_unique(conn)

        n = conn.execute(text("SELECT COUNT(*) FROM coin_pool")).scalar()
        assert n == 2
        by_ex = {
            r[0]: r[1]
            for r in conn.execute(
                text("SELECT exchange, volume_24h FROM coin_pool")
            ).fetchall()
        }
        assert by_ex["binance"] == 2.5e8
        assert by_ex["gate"] == 3.9e7


def test_config_and_status_isolated_per_exchange():
    from app.services.coin_pool_service import CoinPoolService

    svc = CoinPoolService()
    svc.update_config(exchange="binance", max_symbols=25, pool_source="gainers")
    svc.update_config(exchange="gate", max_symbols=40, pool_source="losers")
    assert svc.config_for("binance")["max_symbols"] == 25
    assert svc.config_for("binance")["pool_source"] == "gainers"
    assert svc.config_for("gate")["max_symbols"] == 40
    assert svc.config_for("gate")["pool_source"] == "losers"
    # GATE 改配置不得污染币安
    assert svc.config["max_symbols"] == 25

    svc._set_refresh_status("gate", ok=False, error="gate fail")
    svc._set_refresh_status("binance", ok=True)
    assert svc.status_for("binance")["last_refresh_ok"] is True
    assert svc.status_for("gate")["last_refresh_ok"] is False
    assert "gate fail" in svc.status_for("gate")["last_error"]


def test_orm_allows_same_symbol_on_two_exchanges():
    engine = create_engine("sqlite:///:memory:")
    CoinPool.__table__.create(engine)
    with Session(engine) as session:
        session.add(
            CoinPool(
                exchange="binance",
                symbol="BLESSUSDT",
                rank=1,
                price_change_pct=70.0,
                volume_24h=4.8e8,
                source="gainers",
            )
        )
        session.add(
            CoinPool(
                exchange="gate",
                symbol="BLESSUSDT",
                rank=1,
                price_change_pct=69.0,
                volume_24h=3.1e7,
                source="gainers",
            )
        )
        session.commit()
        rows = session.execute(select(CoinPool)).scalars().all()
    assert len(rows) == 2
    assert {r.exchange for r in rows} == {"binance", "gate"}
