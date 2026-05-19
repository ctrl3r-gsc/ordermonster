import argparse
import asyncio
from pathlib import Path

from sqlalchemy import text

from db import SessionLocal, init_db
from services.catalog import seed_current_catalog


async def migrate(catalog_path: Path) -> None:
    await init_db()
    async with SessionLocal() as session:
        await session.execute(text("TRUNCATE TABLE products RESTART IDENTITY CASCADE;"))
        current_catalog_count = await seed_current_catalog(session, catalog_path)
        await session.commit()

    print(f"Seeded {current_catalog_count} current products. Shops were not created or modified.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the current product catalog into PostgreSQL.")
    parser.add_argument("--catalog", default="data/current_products.json", help="Path to current catalog JSON.")
    args = parser.parse_args()
    asyncio.run(migrate(Path(args.catalog)))


if __name__ == "__main__":
    main()
