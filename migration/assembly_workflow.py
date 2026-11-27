"""Additive migration; old unknown shipping actors remain NULL."""
import asyncio
from sqlalchemy import text
from db.models import AssemblyShipmentCommand
from db.session import engine


async def upgrade_connection(conn):
    for name, kind in (("shipped_by_telegram_id", "BIGINT"), ("shipped_by_name", "VARCHAR(255)"),
                       ("shipped_at", "TIMESTAMPTZ"), ("shipment_source", "VARCHAR(32)")):
        await conn.execute(text(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {name} {kind}"))
    await conn.run_sync(lambda sync: AssemblyShipmentCommand.__table__.create(sync, checkfirst=True))


async def upgrade():
    async with engine.begin() as conn:
        await upgrade_connection(conn)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(upgrade())
