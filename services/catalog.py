import json
import re
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import Product, ProductAlias


DEFAULT_CATALOG_PATH = Path("data/current_products.json")
EXPECTED_CATALOG_SIZE = 13
REQUIRED_CATALOG_FIELDS = {"name", "dosage", "price"}


def slugify_sku_part(value: str | int | None) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


def dosage_label(dosage: int | None) -> str:
    if dosage is None:
        return "na"
    if dosage >= 1000 and dosage % 1000 == 0:
        return f"{dosage // 1000}g"
    return f"{dosage}mg"


def family_slug(name: str) -> str:
    clean = name.lower()
    clean = re.sub(r"\b\d+\s*(?:mg|g)\b", " ", clean)
    clean = re.sub(r"\bthc\b|\bfeco\b", " ", clean)
    flavor_words = ("mango", "green apple", "strawberry", "pineapple", "blackcurrant", "black currant", "watermelon")
    for flavor in flavor_words:
        clean = clean.replace(flavor, " ")
    return slugify_sku_part(clean) or "product"


def make_catalog_sku(name: str, dosage: int | None, flavor: str | None) -> str:
    parts = [family_slug(name)]
    flavor_part = slugify_sku_part(flavor)
    if flavor_part and flavor_part not in parts[0]:
        parts.append(flavor_part)
    parts.append(dosage_label(dosage))
    return "-".join(part for part in parts if part)[:250]


async def seed_current_catalog(session: AsyncSession, path: Path = DEFAULT_CATALOG_PATH) -> int:
    if not path.exists():
        return 0
    products = json.loads(path.read_text(encoding="utf-8"))
    if len(products) != EXPECTED_CATALOG_SIZE:
        raise ValueError(f"Current catalog must contain exactly {EXPECTED_CATALOG_SIZE} products.")
    for item in products:
        missing_fields = REQUIRED_CATALOG_FIELDS - item.keys()
        if missing_fields:
            raise ValueError(f"Catalog item is missing required fields: {', '.join(sorted(missing_fields))}")
        name = item["name"].strip()
        dosage = item.get("dosage")
        flavor = item.get("flavor")
        price_value = Decimal(str(item.get("price") or 0))
        sku = item.get("sku") or make_catalog_sku(name, dosage, flavor)
        product = await session.scalar(select(Product).where(Product.name == name, Product.dosage == dosage))
        if product:
            product.flavor = flavor
            product.potency_type = item.get("potency_type")
            product.sku = sku
            product.price = price_value
            product.is_active = bool(item.get("is_active", True))
            await session.flush()
            product_id = product.id
        else:
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
            result = await session.execute(stmt.returning(Product.id))
            product_id = result.scalar_one()
        for alias in item.get("aliases", []):
            clean_alias = " ".join(str(alias).lower().split())
            if not clean_alias:
                continue
            alias_stmt = insert(ProductAlias).values(
                product_id=product_id,
                alias=clean_alias,
                is_active=True,
            ).on_conflict_do_update(
                index_elements=[ProductAlias.product_id, ProductAlias.alias],
                set_={"is_active": True},
            )
            await session.execute(alias_stmt)
    await session.flush()
    return len(products)


async def active_catalog(session: AsyncSession) -> list[Product]:
    return list(
        (
            await session.scalars(
                select(Product)
                .where(Product.is_active.is_(True))
                .options(selectinload(Product.aliases))
                .order_by(Product.name.asc(), Product.dosage.asc())
            )
        ).all()
    )


async def all_catalog(session: AsyncSession) -> list[Product]:
    return list(
        (
            await session.scalars(
                select(Product)
                .options(selectinload(Product.aliases))
                .order_by(Product.is_active.desc(), Product.name.asc(), Product.dosage.asc())
            )
        ).all()
    )


def catalog_for_parser(products: list[Product]) -> list[dict]:
    return [
        {
            "product_id": product.id,
            "name": product.name,
            "dosage": product.dosage,
            "price": float(product.price or 0),
            "aliases": [alias.alias for alias in product.aliases if alias.is_active],
        }
        for product in products
    ]
