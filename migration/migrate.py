import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import SessionLocal, init_db
from services.catalog import seed_current_catalog


async def migrate(catalog_path: Path) -> None:
    await init_db()
    async with SessionLocal() as session:
        await session.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS display_number INTEGER;"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_display_number ON orders (display_number);"))
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS product_aliases (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    alias VARCHAR(255) NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                );
                """
            )
        )
        await session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_product_aliases_product_alias "
                "ON product_aliases (product_id, alias);"
            )
        )
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_product_aliases_product_id ON product_aliases (product_id);"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_product_aliases_alias ON product_aliases (alias);"))
        current_catalog_count = await seed_current_catalog(session, catalog_path)
        await session.commit()

    print(f"Migrated catalog tables. Upserted {current_catalog_count} current products. Existing orders were not modified.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the current product catalog into PostgreSQL.")
    parser.add_argument("--catalog", default="data/current_products.json", help="Path to current catalog JSON.")
    args = parser.parse_args()
    asyncio.run(migrate(Path(args.catalog)))


if __name__ == "__main__":
    main()
