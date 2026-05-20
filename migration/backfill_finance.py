import asyncio
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import SessionLocal, init_db
from services.finance import backfill_ordermonster_income_transactions


async def main_async() -> None:
    await init_db()
    async with SessionLocal() as session:
        summary = await backfill_ordermonster_income_transactions(session)
        await session.commit()
    print(
        "Backfill summary: "
        f"checked={summary.checked}, "
        f"created={summary.created}, "
        f"updated={summary.updated}, "
        f"skipped={summary.skipped}"
    )


if __name__ == "__main__":
    asyncio.run(main_async())
