import argparse
import asyncio
import json
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import select

from db import SessionLocal, init_db
from db.models import DeliveryStatus, Order, OrderItem, OrderPayment, PaymentMethod, PaymentStatus, Shop
from services.catalog import seed_current_catalog
from services.orders import calculated_unit_price, get_or_create_product, get_or_create_shop
from services.parser import fallback_parse_order_text


FLAVORS = [
    "black currant",
    "green apple",
    "strawberry",
    "mango",
    "watermelon",
    "orange",
    "lemon",
    "blueberry",
    "raspberry",
]


def text_from_message(message: dict[str, Any]) -> str:
    text = message.get("text", "")
    if isinstance(text, list):
        return "".join(part if isinstance(part, str) else part.get("text", "") for part in text)
    return text if isinstance(text, str) else ""


def extract_line_price(line: str) -> Decimal | None:
    if re.search(r"\b(gift|free)\b", line, flags=re.I):
        return None
    matches = re.findall(r"(?:-| )\s*([0-9][0-9,\s]*(?:\.\d+)?)\s*(?:thb)?\s*$", line, flags=re.I)
    if not matches:
        return None
    try:
        return Decimal(matches[-1].replace(",", "").replace(" ", ""))
    except Exception:
        return None


def representative_price(prices: list[Decimal]) -> Decimal:
    filtered = [price for price in prices if Decimal("0") < price <= Decimal("5000")]
    if not filtered:
        return Decimal("0")
    return Decimal(str(median(filtered))).quantize(Decimal("0.01"))


def unit_prices_from_text(text: str) -> dict[tuple[str, int | None, str | None], list[Decimal]]:
    prices: dict[tuple[str, int | None, str | None], list[Decimal]] = defaultdict(list)
    current_product: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        low = line.lower()
        if not line or any(word in low for word in ("total", "paid", "delivered", "waiting", "credit")):
            continue
        dosage_match = re.search(r"(\d{2,4})\s*mg", low)
        qty_match = re.search(r"(?:x\s*(\d+)|(\d+)\s*(?:pcs?|pieces?))", low)
        if not dosage_match and not qty_match:
            current_product = re.sub(r"[^a-zA-Z0-9 -]", "", line).strip().lower() or current_product
            continue
        product_part = re.split(r"\d{2,4}\s*mg|x\s*\d+|\d+\s*(?:pcs?|pieces?)", line, maxsplit=1, flags=re.I)[0]
        product_name = (product_part.strip(" -:") or current_product or "unknown").lower()
        if product_name in {"gummy", "gummies"}:
            product_name = "gummies"
        flavor = next((flavor.title() for flavor in FLAVORS if flavor in low), None)
        quantity = int(next(group for group in qty_match.groups() if group)) if qty_match else 1
        line_price = extract_line_price(line)
        if line_price is not None and quantity > 0:
            prices[(product_name, int(dosage_match.group(1)) if dosage_match else None, flavor)].append(line_price / quantity)
    return prices


def detect_delivery_status(text: str) -> DeliveryStatus:
    low = text.lower()
    if "delivered" in low:
        return DeliveryStatus.delivered
    if "track" in low or "shipped" in low or "sent" in low:
        return DeliveryStatus.shipped
    return DeliveryStatus.pending_shipment


def detect_payment_method(text: str) -> PaymentMethod | None:
    low = text.lower()
    if "cash" in low:
        return PaymentMethod.cash
    if any(word in low for word in ("bank", "transfer", "transaction", "card")):
        return PaymentMethod.transaction
    if any(word in low for word in ("crypto", "usdt", "btc")):
        return PaymentMethod.crypto
    return None


async def migrate(path: Path, catalog_path: Path) -> None:
    await init_db()
    export = json.loads(path.read_text(encoding="utf-8"))
    messages = export.get("messages", [])
    product_prices: dict[tuple[str, int | None, str | None], list[Decimal]] = defaultdict(list)
    order_texts: list[tuple[dict[str, Any], str]] = []

    for message in messages:
        text = text_from_message(message).strip()
        if not text or "total" not in text.lower():
            continue
        order_texts.append((message, text))
        for key, prices in unit_prices_from_text(text).items():
            product_prices[key].extend(prices)

    async with SessionLocal() as session:
        current_catalog_count = await seed_current_catalog(session, catalog_path)
        for (name, dosage, flavor), prices in product_prices.items():
            await get_or_create_product(
                session,
                name,
                dosage,
                flavor,
                representative_price(prices),
                force_update=False,
            )

        created_orders = 0
        for message, text in order_texts:
            parsed = fallback_parse_order_text(text)
            if not parsed.shop_name or not parsed.items:
                continue
            existing_shop = await session.scalar(select(Shop).where(Shop.name == parsed.shop_name.strip()))
            shop = existing_shop or await get_or_create_shop(session, parsed.shop_name)
            order_kwargs = {
                "shop_id": shop.id,
                "user_id": int(str(message.get("from_id", "0")).replace("user", "") or 0),
                "delivery_status": detect_delivery_status(text),
                "total_amount": parsed.total_amount,
            }
            if message.get("date"):
                order_kwargs["created_at"] = datetime.fromisoformat(message["date"])
            order = Order(**order_kwargs)
            session.add(order)
            await session.flush()
            calculated_total = Decimal("0.00")
            for item in parsed.items:
                product = await get_or_create_product(session, item.product_name, item.dosage, item.flavor)
                unit_price = calculated_unit_price(product, shop, item.is_gift)
                if not item.is_gift:
                    calculated_total += Decimal(item.quantity) * unit_price
                session.add(
                    OrderItem(
                        order_id=order.id,
                        product_id=product.id,
                        quantity=item.quantity,
                        price_per_unit=unit_price,
                        is_gift=item.is_gift,
                    )
                )
            if not order.total_amount:
                order.total_amount = calculated_total.quantize(Decimal("0.01"))
            method = detect_payment_method(text)
            if method and order.total_amount:
                session.add(OrderPayment(order_id=order.id, payment_method=method, amount=order.total_amount))
                await session.flush()
                order.payment_status = PaymentStatus.paid
            elif "credit" in text.lower() or "paid" not in text.lower():
                order.payment_status = PaymentStatus.unpaid
            created_orders += 1

        await session.commit()
    print(
        f"Seeded {current_catalog_count} current products, "
        f"analyzed {len(product_prices)} historical product variants, "
        f"and migrated {created_orders} historical orders."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Telegram result.json into PostgreSQL.")
    parser.add_argument("--file", default="result.json", help="Path to Telegram export JSON.")
    parser.add_argument("--catalog", default="data/current_products.json", help="Path to current catalog JSON.")
    args = parser.parse_args()
    asyncio.run(migrate(Path(args.file), Path(args.catalog)))


if __name__ == "__main__":
    main()
