import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from services.parser import parse_order_text


load_dotenv()


def catalog_from_json() -> list[dict]:
    raw_catalog = json.loads(Path("data/current_products.json").read_text(encoding="utf-8"))
    return [
        {
            "product_id": index,
            "name": item["name"],
            "dosage": item.get("dosage"),
            "price": item.get("price"),
            "aliases": item.get("aliases", []),
        }
        for index, item in enumerate(raw_catalog, start=1)
    ]


async def smoke_test() -> None:
    os.environ["GEMINI_API_KEY"] = ""
    result = await parse_order_text(
        "10 pcs gummies 500 king cannabis https://maps.app.goo.gl/Dkoke98UWThuQAp79",
        catalog_products=catalog_from_json(),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(smoke_test())
