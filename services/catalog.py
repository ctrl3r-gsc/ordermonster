import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Product
from services.orders import make_sku


DEFAULT_CATALOG_PATH = Path("data/current_products.json")


async def seed_current_catalog(session: AsyncSession, path: Path = DEFAULT_CATALOG_PATH) -> int:
    if not path.exists():
        return 0
    products = json.loads(path.read_text(encoding="utf-8"))
    for item in products:
        name = item["name"].strip().lower()
        dosage = item.get("dosage")
        flavor = item.get("flavor")
        price_value = Decimal(str(item.get("price") or 0))
        sku = make_sku(name, dosage, flavor)
        stmt = insert(Product).values(
            name=name,
            dosage=dosage,
            flavor=flavor,
            potency_type=item.get("potency_type"),
            sku=sku,
            price=price_value,
            is_active=bool(item.get("is_active", True)),
        ).on_conflict_do_update(
            index_elements=[Product.sku],
            set_={
                "name": name,
                "dosage": dosage,
                "flavor": flavor,
                "potency_type": item.get("potency_type"),
                "price": price_value,
                "is_active": bool(item.get("is_active", True)),
            },
        )
        await session.execute(stmt)
    await session.flush()
    return len(products)
