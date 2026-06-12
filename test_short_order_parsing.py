import asyncio
import os

from services.parser import parse_order_text


SHORT_ORDER_CATALOG = [
    {
        "product_id": 11,
        "name": "BROWNIE 100mg THC",
        "dosage": 100,
        "price": 100,
        "aliases": ["brownie", "brownie 100", "brownie 100mg"],
    },
    {
        "product_id": 12,
        "name": "COOKIES 100mg THC",
        "dosage": 100,
        "price": 100,
        "aliases": ["cookies", "cookies 100", "cookies 100mg"],
    },
    {
        "product_id": 21,
        "name": "ULTIMATE GUMMIES MANGO 100mg",
        "dosage": 100,
        "price": 80,
        "aliases": ["gummies", "gummies 100", "ultimate gummies 100"],
    },
    {
        "product_id": 24,
        "name": "ULTIMATE GUMMIES GREEN APPLE 250mg",
        "dosage": 250,
        "price": 170,
        "aliases": ["gummies 250", "gummies 250mg", "250 gummies", "ultimate gummies 250"],
    },
    {
        "product_id": 25,
        "name": "ROSIN GUMMIES GREEN APPLE 250mg",
        "dosage": 250,
        "price": 300,
        "aliases": ["gummies rosin", "rosin gummies", "rosin", "apple rosin", "green apple rosin"],
    },
    {
        "product_id": 22,
        "name": "X-HASH GUMMIES PINEAPPLE 150mg",
        "dosage": 150,
        "price": 90,
        "aliases": ["gummies 150", "x-hash gummies 150"],
    },
    {
        "product_id": 23,
        "name": "ULTIMATE GUMMIES STRAWBERRY 500mg",
        "dosage": 500,
        "price": 270,
        "aliases": ["gummies 500", "ultimate gummies 500"],
    },
    {
        "product_id": 26,
        "name": "X-HASH GUMMIES BLACKCURRANT 350mg",
        "dosage": 350,
        "price": 230,
        "aliases": ["gummies 350", "350 gummies", "gummies 350mg", "blackcurrant 350"],
    },
]


def parse(text: str) -> dict:
    os.environ["GEMINI_API_KEY"] = ""
    return asyncio.run(parse_order_text(text, catalog_products=SHORT_ORDER_CATALOG))


def test_brownie_dosage_quantity_short_form() -> None:
    result = parse("brownie 100 20")

    assert result["shop_name"] is None
    assert result["items"][0]["product_id"] == 11
    assert result["items"][0]["quantity"] == 20


def test_gummies_dosage_quantity_short_form() -> None:
    result = parse("gummies 150 10")

    assert result["shop_name"] is None
    assert result["items"][0]["product_id"] == 22
    assert result["items"][0]["quantity"] == 10


def test_multi_item_short_lines_stay_separate() -> None:
    result = parse("gummies 100 6\nbrownie 100 20")

    assert result["shop_name"] is None
    assert len(result["items"]) == 2
    assert result["items"][0]["product_id"] == 21
    assert result["items"][0]["quantity"] == 6
    assert result["items"][1]["product_id"] == 11
    assert result["items"][1]["quantity"] == 20


def test_generic_gummies_with_one_number_needs_clarification() -> None:
    result = parse("gummies 10")

    assert result["shop_name"] is None
    assert result["items"][0]["product_id"] is None
    assert result["unresolved_products"][0]["similar_product_ids"]


def test_complex_multi_item_order_keeps_line_clarification_scoped() -> None:
    result = parse(
        "gummies rosin 4pcs\n"
        "gummies 350mg 5pcs\n"
        "gummies 250mg 4pcs\n"
        "brownie 100mg 5pcs\n"
        "cookies 100mg 2pcs"
    )

    assert result["shop_name"] is None
    assert len(result["items"]) == 5
    assert [item["quantity"] for item in result["items"]] == [4, 5, 4, 5, 2]
    assert result["items"][0]["product_id"] == 25
    assert result["items"][1]["product_id"] == 26
    assert result["items"][2]["product_id"] is None
    assert result["items"][3]["product_id"] == 11
    assert result["items"][4]["product_id"] == 12

    unresolved = result["unresolved_products"]
    assert len(unresolved) == 1
    assert unresolved[0]["item_index"] == 2
    assert unresolved[0]["line_index"] == 3
    assert unresolved[0]["original_text"] == "gummies 250mg 4pcs"
    assert set(unresolved[0]["similar_product_ids"]) == {24, 25}


def test_gummies_350_dosage_quantity_short_form() -> None:
    result = parse("gummies 350 5")

    assert result["shop_name"] is None
    assert result["items"][0]["product_id"] == 26
    assert result["items"][0]["quantity"] == 5


def test_gummies_250_without_line_keyword_needs_clarification() -> None:
    result = parse("gummies 250 4")

    assert result["shop_name"] is None
    assert result["items"][0]["product_id"] is None
    assert result["items"][0]["quantity"] == 4
    assert result["unresolved_products"][0]["line_index"] == 1
    assert set(result["unresolved_products"][0]["similar_product_ids"]) == {24, 25}


def test_paid_and_gift_same_product_stay_separate() -> None:
    result = parse("rosin gummies 3pcs\nrosin gummies 1pc free")

    assert result["shop_name"] is None
    assert len(result["items"]) == 2
    assert result["items"][0]["product_id"] == 25
    assert result["items"][0]["quantity"] == 3
    assert result["items"][0]["is_gift"] is False
    assert result["items"][1]["product_id"] == 25
    assert result["items"][1]["quantity"] == 1
    assert result["items"][1]["is_gift"] is True


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
