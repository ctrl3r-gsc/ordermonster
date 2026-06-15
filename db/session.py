from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings
from db.models import Base

settings = get_settings()
engine: AsyncEngine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS display_number INTEGER"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_display_number ON orders (display_number)"))
        await conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS notification_sent_at TIMESTAMPTZ NULL"))
        await conn.execute(text("UPDATE orders SET display_number = id WHERE display_number IS NULL"))


async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
