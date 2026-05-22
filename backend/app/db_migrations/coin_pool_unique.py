"""SQLite: coin_pool.symbol 全局唯一 → (symbol, source) 联合唯一。"""

from sqlalchemy import inspect, text


def _index_columns(sync_conn, index_name: str) -> list[str]:
    rows = sync_conn.execute(text(f'PRAGMA index_info("{index_name}")')).fetchall()
    return [r[2] for r in rows]


def migrate_coin_pool_symbol_source_unique(sync_conn) -> None:
    insp = inspect(sync_conn)
    if "coin_pool" not in insp.get_table_names():
        return

    has_composite = False
    symbol_only_unique = False
    for idx in insp.get_indexes("coin_pool"):
        if not idx.get("unique"):
            continue
        cols = idx.get("column_names") or []
        if cols == ["symbol", "source"]:
            has_composite = True
        elif cols == ["symbol"]:
            symbol_only_unique = True

    if has_composite and not symbol_only_unique:
        return
    if not symbol_only_unique and not has_composite:
        # 无唯一索引的旧表：直接加联合唯一（重建）
        pass

    sync_conn.execute(text("PRAGMA foreign_keys=OFF"))
    sync_conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS coin_pool_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol VARCHAR(50) NOT NULL,
                rank INTEGER NOT NULL,
                price_change_pct FLOAT NOT NULL,
                volume_24h FLOAT,
                source VARCHAR(20) NOT NULL,
                added_at DATETIME,
                last_updated DATETIME,
                CONSTRAINT uq_coinpool_symbol_source UNIQUE (symbol, source)
            )
            """
        )
    )
    sync_conn.execute(
        text(
            """
            INSERT OR IGNORE INTO coin_pool_new
                (id, symbol, rank, price_change_pct, volume_24h, source, added_at, last_updated)
            SELECT id, symbol, rank, price_change_pct, volume_24h, source, added_at, last_updated
            FROM coin_pool
            """
        )
    )
    sync_conn.execute(text("DROP TABLE coin_pool"))
    sync_conn.execute(text("ALTER TABLE coin_pool_new RENAME TO coin_pool"))
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS idx_coinpool_source ON coin_pool (source)"))
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS idx_coinpool_rank ON coin_pool (rank)"))
    sync_conn.execute(text("PRAGMA foreign_keys=ON"))
