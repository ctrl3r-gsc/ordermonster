import argparse
import asyncio
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from db import SessionLocal, init_db
from db.models import DeliveryStatus, Order, OrderItem, OrderPayment, PaymentMethod, PaymentStatus, Shop
from services.catalog import seed_current_catalog
from services.orders import calculated_unit_price, find_product_for_item, get_or_create_shop
from services.parser import fallback_parse_order_text


def text_from_message(message: dict[str, Any]) -> str:
    text = message.get("text", "")
    if isinstance(text, list):
        return "".join(part if isinstance(part, str) else part.get("text", "") for part in text)
    return text if isinstance(text, str) else ""


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
    async with SessionLocal() as session:
        await session.execute(text("TRUNCATE TABLE products RESTART IDENTITY CASCADE;"))
        current_catalog_count = await seed_current_catalog(session, catalog_path)

        export = json.loads(path.read_text(encoding="utf-8"))
        messages = export.get("messages", [])
        order_texts: list[tuple[dict[str, Any], str]] = []

        for message in messages:
            text_value = text_from_message(message).strip()
            if text_value and "total" in text_value.lower():
                order_texts.append((message, text_value))

        created_orders = 0
        skipped_items = 0
        for message, text in order_texts:
            parsed = fallback_parse_order_text(text)
            if not parsed.shop_name or not parsed.items:
                continue
            existing_shop = await session.scalar(select(Shop).where(Shop.name == parsed.shop_name.strip()))
            shop = existing_shop or await get_or_create_shop(session, parsed.shop_name)
            matched_items = []
            for item in parsed.items:
                product = await find_product_for_item(session, item.product_name, item.dosage, item.flavor)
                if product is None:
                    skipped_items += 1
                    continue
                matched_items.append((item, product))
            if not matched_items:
                continue
            order_kwargs = {
                "shop_id": shop.id,
                "user_id": int(str(message.get("from_id", "0")).replace("user", "") or 0),
                "delivery_status": detect_delivery_status(text),
                "total_amount": Decimal("0.00"),
            }
            if message.get("date"):
                order_kwargs["created_at"] = datetime.fromisoformat(message["date"])
            order = Order(**order_kwargs)
            session.add(order)
            await session.flush()
            calculated_total = Decimal("0.00")
            for item, product in matched_items:
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
        f"migrated {created_orders} historical orders, "
        f"and skipped {skipped_items} items that were not in the clean catalog."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Telegram result.json into PostgreSQL.")
    parser.add_argument("--file", default="result.json", help="Path to Telegram export JSON.")
    parser.add_argument("--catalog", default="data/current_products.json", help="Path to current catalog JSON.")
    args = parser.parse_args()
    asyncio.run(migrate(Path(args.file), Path(args.catalog)))


if __name__ == "__main__":
    main()
