from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.database import Base
from app.models.strategy import Strategy
from app.models.position import Position
from app.models.trade import Trade
from app.models.account import Account
from app.models.coin_pool import CoinPool
from app.models.bot_config import BotConfig

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    # Use sync driver for offline mode
    url = url.replace("sqlite+aiosqlite:///", "sqlite:///")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # Rewrite URL for sync driver
    url = config.get_main_option("sqlalchemy.url").replace("sqlite+aiosqlite:///", "sqlite:///")
    from sqlalchemy import create_engine
    sync_engine = create_engine(url)
    with sync_engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
