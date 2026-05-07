import logging
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


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    # Create tables from current model (no-op if already exist)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add new columns that may not exist in existing databases
        migrations = [
            "ALTER TABLE strategies ADD COLUMN wt_ob_level FLOAT DEFAULT 60.0",
            "ALTER TABLE strategies ADD COLUMN wt_os_level FLOAT DEFAULT -60.0",
        ]
        for sql in migrations:
            try:
                await conn.run_sync(lambda c, s=sql: c.exec_driver_sql(s))
            except Exception:
                pass  # column already exists
