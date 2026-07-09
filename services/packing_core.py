from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from html import escape
from typing import Iterable


MAX_TELEGRAM_MESSAGE_LENGTH = 3900


def packing_status_icon(order) -> str:
    if getattr(order, "delivery_status", None) and getattr(order.delivery_status, "value", order.delivery_status) == "delivered":
        return "✅"
    return "⏳"


def packing_date(value: datetime | None, formatter) -> str:
    if value is None:
        return "unknown"
    formatted = formatter(value)
    return formatted.replace(" (", " ").replace(")", "")


def packing_product_name(product) -> str:
    name = " ".join(str(getattr(product, "name", "Item") or "Item").split())
    flavor = getattr(product, "flavor", None)
    if flavor:
        name = f"{name} ({flavor})"
    return name


def grouped_order_items(order) -> list[tuple[str, int, bool]]:
    grouped: dict[tuple[str, bool], int] = defaultdict(int)
    for item in getattr(order, "items", []) or []:
        product = getattr(item, "product", None)
        if product is None:
            continue
        quantity = int(getattr(item, "quantity", 0) or 0)
        if quantity <= 0:
            continue
        is_gift = bool(getattr(item, "is_gift", False))
        grouped[(packing_product_name(product), is_gift)] += quantity
    return [
        (name, quantity, is_gift)
        for (name, is_gift), quantity in sorted(grouped.items(), key=lambda entry: (entry[0][0].lower(), entry[0][1]))
    ]


def total_packing_items(orders: Iterable) -> list[tuple[str, int, bool]]:
    grouped: dict[tuple[str, bool], int] = defaultdict(int)
    for order in orders:
        for name, quantity, is_gift in grouped_order_items(order):
            grouped[(name, is_gift)] += quantity
    return [
        (name, quantity, is_gift)
        for (name, is_gift), quantity in sorted(grouped.items(), key=lambda entry: (entry[0][0].lower(), entry[0][1]))
    ]


def item_line(name: str, quantity: int, is_gift: bool = False) -> str:
    suffix = " gift" if is_gift else ""
    return f"• {escape(name)} — {quantity} pcs{suffix}"


def packing_order_block(order, date_formatter) -> str | None:
    items = grouped_order_items(order)
    if not items:
        return None
    display_number = getattr(order, "display_number", None) or getattr(order, "id", "?")
    shop = getattr(order, "shop", None)
    shop_name = escape(str(getattr(shop, "name", "Unknown shop") or "Unknown shop"))
    date = packing_date(getattr(order, "created_at", None), date_formatter)
    lines = [f"#{display_number} | {packing_status_icon(order)} {shop_name} | {date}"]
    lines.extend(item_line(name, quantity, is_gift) for name, quantity, is_gift in items)
    return "\n".join(lines)


def packing_list_text(orders: list, date_formatter) -> str:
    packable_orders = [order for order in orders if grouped_order_items(order)]
    if not packable_orders:
        return "📦 <b>Packing List</b>\n\nNothing to prepare right now."

    lines = [
        "📦 <b>Packing List</b>",
        "",
        f"Orders to prepare: <b>{len(packable_orders)}</b>",
        f"Boxes: <b>{len(packable_orders)}</b>",
        "",
        "<b>TOTAL TO PACK:</b>",
    ]
    lines.extend(item_line(name, quantity, is_gift) for name, quantity, is_gift in total_packing_items(packable_orders))
    lines.extend(["", "<b>BY SHOP:</b>"])
    for order in packable_orders:
        block = packing_order_block(order, date_formatter)
        if block:
            lines.extend(["", block])
    return "\n".join(lines)


def split_packing_messages(text: str, max_length: int = MAX_TELEGRAM_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= max_length:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block
    if current:
        chunks.append(current)
    return chunks
