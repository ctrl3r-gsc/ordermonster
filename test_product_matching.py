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


if __name__ == "__main__":
    asyncio.run(run_tests())
