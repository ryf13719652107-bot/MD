"""SQLite: coin_pool 唯一约束 (symbol, source) → (exchange, symbol, source)。"""

from sqlalchemy import inspect, text


def migrate_coin_pool_exchange_unique(sync_conn) -> None:
    insp = inspect(sync_conn)
    if "coin_pool" not in insp.get_table_names():
        return

    cols = {c["name"] for c in insp.get_columns("coin_pool")}
    has_exchange = "exchange" in cols

    def _cols_match(names) -> bool:
        return list(names or []) == ["exchange", "symbol", "source"]

    has_triple = False
    # SQLite 上 UniqueConstraint 多出现在 unique_constraints；部分环境也会进 indexes
    for idx in insp.get_indexes("coin_pool"):
        if idx.get("unique") and _cols_match(idx.get("column_names")):
            has_triple = True
            break
    if not has_triple:
        for uc in insp.get_unique_constraints("coin_pool"):
            if _cols_match(uc.get("column_names")):
                has_triple = True
                break
    if not has_triple:
        # 再查 sqlite_master，避免误判导致每次启动重建表
        row = sync_conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='coin_pool'"
            )
        ).fetchone()
        ddl = (row[0] or "") if row else ""
        if "uq_coinpool_exchange_symbol_source" in ddl or (
            "UNIQUE" in ddl.upper()
            and "exchange" in ddl
            and "symbol" in ddl
            and "source" in ddl
        ):
            has_triple = True

    if has_exchange and has_triple:
        return

    sync_conn.execute(text("PRAGMA foreign_keys=OFF"))
    sync_conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS coin_pool_ex_new (
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
    )
    if has_exchange:
        sync_conn.execute(
            text(
                """
                INSERT OR IGNORE INTO coin_pool_ex_new
                    (id, exchange, symbol, rank, price_change_pct, volume_24h,
                     source, added_at, last_updated)
                SELECT id, COALESCE(exchange, 'binance'), symbol, rank,
                       price_change_pct, volume_24h, source, added_at, last_updated
                FROM coin_pool
                """
            )
        )
    else:
        sync_conn.execute(
            text(
                """
                INSERT OR IGNORE INTO coin_pool_ex_new
                    (id, exchange, symbol, rank, price_change_pct, volume_24h,
                     source, added_at, last_updated)
                SELECT id, 'binance', symbol, rank, price_change_pct, volume_24h,
                       source, added_at, last_updated
                FROM coin_pool
                """
            )
        )
    sync_conn.execute(text("DROP TABLE coin_pool"))
    sync_conn.execute(text("ALTER TABLE coin_pool_ex_new RENAME TO coin_pool"))
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS idx_coinpool_source ON coin_pool (source)"))
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS idx_coinpool_rank ON coin_pool (rank)"))
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS idx_coinpool_exchange ON coin_pool (exchange)"))
    sync_conn.execute(text("PRAGMA foreign_keys=ON"))
