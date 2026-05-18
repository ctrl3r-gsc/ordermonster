import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from services.orders import get_or_create_product


DEFAULT_CATALOG_PATH = Path("data/current_products.json")


async def seed_current_catalog(session: AsyncSession, path: Path = DEFAULT_CATALOG_PATH) -> int:
    if not path.exists():
        return 0
    products = json.loads(path.read_text(encoding="utf-8"))
    for item in products:
        await get_or_create_product(
            session,
            item["name"],
            item.get("dosage"),
            item.get("flavor"),
            price=item.get("price") or 0,
            potency_type=item.get("potency_type"),
            is_active=bool(item.get("is_active", True)),
        )
    return len(products)
