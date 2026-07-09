from dataclasses import dataclass
from datetime import datetime, timezone

from services.packing_core import (
    PACKING_MARKERS,
    grouped_order_items,
    packing_list_text,
    split_packing_messages,
    total_packing_items,
)


@dataclass
class Product:
    name: str
    flavor: str | None = None


@dataclass
class Item:
    product: Product
    quantity: int
    is_gift: bool = False


@dataclass
class Shop:
    name: str


@dataclass
class Status:
    value: str


@dataclass
class Order:
    id: int
    display_number: int
    shop: Shop
    items: list[Item]
    created_at: datetime
    delivery_status: Status


def date_formatter(value: datetime) -> str:
    return value.strftime("%d.%m %H:%M")


def make_order(display_number: int, shop_name: str, items: list[Item], status: str = "pending_shipment") -> Order:
    return Order(
        id=display_number,
        display_number=display_number,
        shop=Shop(shop_name),
        items=items,
        created_at=datetime(2026, 7, 9, 9, 34, tzinfo=timezone.utc),
        delivery_status=Status(status),
    )


def test_quantities_aggregate_in_total_to_pack() -> None:
    brownie = Product("Brownie 100mg")
    orders = [
        make_order(68, "KING CANNABIS", [Item(brownie, 10)]),
        make_order(69, "BAAN PIN HOTEL", [Item(brownie, 5)]),
    ]

    assert total_packing_items(orders) == [("Brownie 100mg", 15, False)]


def test_shop_block_groups_duplicate_items_and_keeps_gifts_separate() -> None:
    brownie = Product("Brownie 100mg")
    order = make_order(
        68,
        "KING CANNABIS",
        [Item(brownie, 10), Item(brownie, 3), Item(brownie, 2, is_gift=True)],
    )

    assert grouped_order_items(order) == [
        ("Brownie 100mg", 13, False),
        ("Brownie 100mg", 2, True),
    ]


def test_packing_list_text_contains_summary_and_shop_blocks() -> None:
    brownie = Product("Brownie 100mg")
    gummies = Product("Gummies", "Mango")
    orders = [
        make_order(68, "KING CANNABIS", [Item(brownie, 10), Item(gummies, 2, is_gift=True)]),
    ]

    text = packing_list_text(orders, date_formatter)

    assert "📦 <b>Packing List</b>" in text
    assert "Orders to prepare: <b>1</b>" in text
    assert "Boxes: <b>1</b>" in text
    assert "• Brownie 100mg — 10 pcs" in text
    assert "• Gummies (Mango) — 2 pcs gift" in text
    assert "🟥 #68 | ⏳ KING CANNABIS | 09.07 09:34" in text
    assert "🟥 • Brownie 100mg — 10 pcs" in text
    assert "🟥 • Gummies (Mango) — 2 pcs gift" in text


def test_packing_list_cycles_order_markers() -> None:
    brownie = Product("Brownie 100mg")
    orders = [
        make_order(index + 1, f"SHOP {index + 1}", [Item(brownie, 1)])
        for index in range(len(PACKING_MARKERS) + 1)
    ]

    text = packing_list_text(orders, date_formatter)

    assert "🟥 #1 | ⏳ SHOP 1 | 09.07 09:34" in text
    assert "⬛️ #8 | ⏳ SHOP 8 | 09.07 09:34" in text
    assert "🟥 #9 | ⏳ SHOP 9 | 09.07 09:34" in text


def test_no_pending_orders_message_is_compact() -> None:
    assert packing_list_text([], date_formatter) == "📦 <b>Packing List</b>\n\nNothing to prepare right now."


def test_split_packing_messages_keeps_short_text_single_message() -> None:
    assert split_packing_messages("short", max_length=10) == ["short"]


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
