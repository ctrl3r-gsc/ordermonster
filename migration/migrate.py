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
from services.finance import backfill_ordermonster_income_transactions


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
        await session.execute(
            text(
                """
                DO $$ BEGIN
                    CREATE TYPE company_transaction_type AS ENUM ('income', 'expense');
                EXCEPTION
                    WHEN duplicate_object THEN null;
                END $$;
                """
            )
        )
        await session.execute(
            text(
                """
                DO $$ BEGIN
                    CREATE TYPE company_transaction_source_bot AS ENUM ('ordermonster', 'expense_bot', 'manual');
                EXCEPTION
                    WHEN duplicate_object THEN null;
                END $$;
                """
            )
        )
        await session.execute(
            text(
                """
                DO $$ BEGIN
                    CREATE TYPE company_transaction_payment_method AS ENUM ('cash', 'transfer', 'crypto', 'unknown');
                EXCEPTION
                    WHEN duplicate_object THEN null;
                END $$;
                """
            )
        )
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS company_transactions (
                    id SERIAL PRIMARY KEY,
                    type company_transaction_type NOT NULL,
                    source_bot company_transaction_source_bot NOT NULL,
                    category VARCHAR(120) NOT NULL,
                    amount NUMERIC(12, 2) NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'THB',
                    payment_method company_transaction_payment_method NOT NULL DEFAULT 'unknown',
                    related_order_id INTEGER NULL REFERENCES orders(id) ON DELETE SET NULL,
                    description TEXT NULL,
                    transaction_date TIMESTAMPTZ NOT NULL DEFAULT now(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
        )
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_company_transactions_type ON company_transactions (type);"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_company_transactions_source_bot ON company_transactions (source_bot);"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_company_transactions_category ON company_transactions (category);"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_company_transactions_payment_method ON company_transactions (payment_method);"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_company_transactions_related_order_id ON company_transactions (related_order_id);"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_company_transactions_transaction_date ON company_transactions (transaction_date);"))
        await session.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_company_transactions_ordermonster_order
                ON company_transactions (source_bot, related_order_id)
                WHERE source_bot = 'ordermonster'
                  AND type = 'income'
                  AND related_order_id IS NOT NULL;
                """
            )
        )
        current_catalog_count = await seed_current_catalog(session, catalog_path)
        backfill_summary = await backfill_ordermonster_income_transactions(session)
        await session.commit()

    print(f"Migrated catalog and finance tables. Upserted {current_catalog_count} current products.")
    print(
        "Backfill summary: "
        f"checked={backfill_summary.checked}, "
        f"created={backfill_summary.created}, "
        f"updated={backfill_summary.updated}, "
        f"skipped={backfill_summary.skipped}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the current product catalog into PostgreSQL.")
    parser.add_argument("--catalog", default="data/current_products.json", help="Path to current catalog JSON.")
    args = parser.parse_args()
    asyncio.run(migrate(Path(args.catalog)))


if __name__ == "__main__":
    main()
