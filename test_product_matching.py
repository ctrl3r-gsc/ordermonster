import asyncio
import os

from services.parser import parse_order_text


TEST_CATALOG = [
    {
        "product_id": 101,
        "name": "LINE A GUMMIES 100mg",
        "dosage": 100,
        "price": 80,
        "aliases": ["line a gummies", "line a gummies 100", "line a"],
    },
    {
        "product_id": 102,
        "name": "LINE A GUMMIES 500mg",
        "dosage": 500,
        "price": 270,
        "aliases": ["line a gummies 500", "line a 500"],
    },
    {
        "product_id": 201,
        "name": "LINE B GUMMIES 100mg",
        "dosage": 100,
        "price": 90,
        "aliases": ["line b gummies", "line b gummies 100", "line b"],
    },
    {
        "product_id": 301,
        "name": "ITEM A 100mg",
        "dosage": 100,
        "price": 70,
        "aliases": ["item a", "item a 100mg"],
    },
    {
        "product_id": 302,
        "name": "ITEM B",
        "dosage": None,
        "price": 60,
        "aliases": ["item b"],
    },
    {
        "product_id": 401,
        "name": "PRODUCT 100mg",
        "dosage": 100,
        "price": 70,
        "aliases": ["product", "product 100mg"],
    },
    {
        "product_id": 402,
        "name": "ANOTHER PRODUCT",
        "dosage": None,
        "price": 60,
        "aliases": ["another product"],
    },
]


async def run_tests() -> None:
    os.environ["GEMINI_API_KEY"] = ""
    line_a = await parse_order_text("line A gummies 1pc", catalog_products=TEST_CATALOG)
    assert line_a["shop_name"] is None
    assert line_a["items"][0]["quantity"] == 1
    assert line_a["items"][0]["product_id"] is None
    assert set(line_a["unresolved_products"][0]["similar_product_ids"]) <= {101, 102}

    generic = await parse_order_text("gummies 1pc", catalog_products=TEST_CATALOG)
    assert generic["shop_name"] is None
    assert generic["items"][0]["quantity"] == 1
    assert generic["items"][0]["product_id"] is None
    assert generic["unresolved_products"][0]["similar_product_ids"]

    dosage = await parse_order_text("line A gummies 500 10 pcs shop name", catalog_products=TEST_CATALOG)
    assert dosage["items"][0]["product_id"] == 102
    assert dosage["items"][0]["quantity"] == 10
    assert dosage["shop_name"] == "SHOP NAME"

    multi_items = await parse_order_text("item A 100mg 6pc\nitem B 20pcs", catalog_products=TEST_CATALOG)
    assert multi_items["shop_name"] is None
    assert len(multi_items["items"]) == 2
    assert multi_items["items"][0]["product_id"] == 301
    assert multi_items["items"][0]["quantity"] == 6
    assert multi_items["items"][1]["product_id"] == 302
    assert multi_items["items"][1]["quantity"] == 20

    product_items = await parse_order_text("product 100mg 6pc\nanother product 20pcs", catalog_products=TEST_CATALOG)
    assert product_items["shop_name"] is None
    assert len(product_items["items"]) == 2
    assert product_items["items"][0]["product_id"] == 401
    assert product_items["items"][0]["quantity"] == 6
    assert product_items["items"][1]["product_id"] == 402
    assert product_items["items"][1]["quantity"] == 20


if __name__ == "__main__":
    asyncio.run(run_tests())
