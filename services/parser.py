import json
import os
import re
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from config import get_settings


class OrderItem(BaseModel):
    product_name: str
    dosage: int | None = None
    flavor: str | None = None
    quantity: int = Field(ge=1)
    is_gift: bool = False


class ExtractedOrder(BaseModel):
    shop_name: str | None = None
    items: list[OrderItem] = Field(default_factory=list)
    suggested_payment_method: Literal["cash", "transaction", "crypto"] | None = None
    total_amount: float | None = None


ExtractedOrderItem = OrderItem
OrderExtractionModel = ExtractedOrder


SYSTEM_INSTRUCTION = (
    "You are a precise CRM assistant for a confectionery and bar business order tracking system. "
    "Your job is to parse messy, unstructured text messages into a strict JSON schema.\n\n"
    "CRITICAL RULES:\n"
    "1. `shop_name`: Extract ONLY the specific name of the shop/client (e.g., 'шаман', 'SHAMAN', 'TAI MA TON'). "
    "NEVER copy the whole text here! If no shop name is mentioned in the text, set it to null.\n"
    "2. `items`: Extract every single ordered product into this array.\n"
    "   - `product_name`: Standardize to 'Gummies', 'Brownie', 'Cookie', or 'Drops' (e.g., 'гамми', 'гамме' -> 'Gummies').\n"
    "   - `dosage`: Extract ONLY the integer number of milligrams (e.g., '500мг' -> 500). If not mentioned -> null.\n"
    "   - `flavor`: Extract the flavor string (e.g., 'клубника', 'strawberry'). If not mentioned -> null.\n"
    "   - `quantity`: Extract the exact integer count.\n"
    "   - `is_gift`: Set to true ONLY if words like 'бонус', 'подарок', 'на пробу', 'gift' are near the item.\n"
    "3. `suggested_payment_method`: Strictly 'cash', 'transaction', 'crypto', or null.\n"
    "4. `total_amount`: Extract the numeric total price if explicitly provided at the end (e.g., '3000' or '3,000').\n\n"
    "EXAMPLES OF CORRECT PARSING:\n\n"
    "Input: 'бро привет, запиши нам 10 пачек гамми 500мг клубника в шаман, оплата налик'\n"
    "Output:\n"
    "{\n"
    "  \"shop_name\": \"шаман\",\n"
    "  \"items\": [\n"
    "    {\"product_name\": \"Gummies\", \"dosage\": 500, \"flavor\": \"клубника\", \"quantity\": 10, \"is_gift\": false}\n"
    "  ],\n"
    "  \"suggested_payment_method\": \"cash\",\n"
    "  \"total_amount\": null\n"
    "}\n\n"
    "Input: 'TAI MA TON\\nbrownie\\n100mg x30 - 3,000\\n500mg x1 - gift\\ntotal: 3,000 thb paid cash'\n"
    "Output:\n"
    "{\n"
    "  \"shop_name\": \"TAI MA TON\",\n"
    "  \"items\": [\n"
    "    {\"product_name\": \"Brownie\", \"dosage\": 100, \"flavor\": null, \"quantity\": 30, \"is_gift\": false},\n"
    "    {\"product_name\": \"Brownie\", \"dosage\": 500, \"flavor\": null, \"quantity\": 1, \"is_gift\": true}\n"
    "  ],\n"
    "  \"suggested_payment_method\": \"cash\",\n"
    "  \"total_amount\": 3000\n"
    "}\n"
)


PRODUCT_ALIASES = {
    "gummy": "Gummies",
    "gummies": "Gummies",
    "гамми": "Gummies",
    "гамме": "Gummies",
    "brownie": "Brownie",
    "брауни": "Brownie",
    "cookie": "Cookie",
    "cookies": "Cookie",
    "печенье": "Cookie",
    "drop": "Drops",
    "drops": "Drops",
    "капли": "Drops",
}


FLAVORS = [
    "black currant",
    "blackcurrant",
    "green apple",
    "strawberry",
    "pineapple",
    "mango",
    "watermelon",
    "orange",
    "lemon",
    "blueberry",
    "raspberry",
    "смородина",
    "яблоко",
    "клубника",
    "ананас",
    "манго",
    "арбуз",
]


def _normalize_payment(text: str) -> str | None:
    low = text.lower()
    if any(word in low for word in ("crypto", "крипта", "usdt", "btc")):
        return "crypto"
    if any(word in low for word in ("cash", "налик", "наличные")):
        return "cash"
    if any(word in low for word in ("bank", "transfer", "transaction", "card", "перевод", "карта", "банк", "транзакция")):
        return "transaction"
    return None


def _to_number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _standardize_product_name(value: str) -> str:
    low = value.lower().strip()
    for alias, normalized in PRODUCT_ALIASES.items():
        if alias in low:
            return normalized
    return value.strip() or "unknown"


def _parse_dosage(match: re.Match[str] | None) -> int | None:
    if not match:
        return None
    amount = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    return int(amount * 1000) if unit in {"g", "г", "гр"} else int(amount)


def _extract_inline_shop_name(text: str) -> str | None:
    patterns = [
        r"(?:\bв|для|to|for)\s+([a-zA-Zа-яА-Я0-9 ._-]{2,40}?)(?:[,.;]|\s+(?:оплата|paid|налик|наличные|перевод|карта|банк|total|итого)|$)",
        r"(?:shop|client|клиент|магазин)\s*:?\s*([a-zA-Zа-яА-Я0-9 ._-]{2,40})(?:[,.;]|\n|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1).strip(" -:,.") or None
    return None


def _line_looks_like_shop(line: str) -> bool:
    low = line.lower()
    has_order_signal = bool(re.search(r"\d|\b(mg|мг|g|гр|г|x|pcs?|шт|пач)", low))
    has_sentence_signal = any(word in low for word in ("привет", "запиши", "оплата", "paid", "total", "итого"))
    return len(line) <= 80 and not has_order_signal and not has_sentence_signal


def _extract_quantity(line: str) -> int:
    low = line.lower()
    patterns = [
        r"(\d+)\s*(?:пачек|пачки|packs?|pcs?|pieces?|шт|штук|уп)",
        r"x\s*(\d+)",
        r"(\d+)\s*x",
    ]
    for pattern in patterns:
        match = re.search(pattern, low, flags=re.I)
        if match:
            return int(match.group(1))
    return 1


def _has_quantity_signal(line: str) -> bool:
    low = line.lower()
    return any(
        re.search(pattern, low, flags=re.I)
        for pattern in (
            r"\d+\s*(?:пачек|пачки|packs?|pcs?|pieces?|шт|штук|уп)",
            r"x\s*\d+",
            r"\d+\s*x",
        )
    )


def _extract_product_name(line: str, current_product: str | None) -> str:
    low = line.lower()
    for alias in PRODUCT_ALIASES:
        if alias in low:
            return PRODUCT_ALIASES[alias]
    return current_product or "unknown"


def _parse_item_line(line: str, current_product: str | None) -> tuple[OrderItem | None, str | None]:
    low = line.lower()
    dosage_match = re.search(r"(\d+(?:[\.,]\d+)?)\s*(mg|мг|g|гр|г)(?![a-zа-я])", low)
    has_product = any(alias in low for alias in PRODUCT_ALIASES)
    has_quantity = _has_quantity_signal(line)
    if not dosage_match and not has_quantity:
        clean_product = re.sub(r"[^a-zA-Zа-яА-Я0-9 -]", "", line).strip()
        return None, _standardize_product_name(clean_product) if clean_product else current_product

    product_name = _extract_product_name(line, current_product)
    flavor = next((flavor for flavor in FLAVORS if flavor in low), None)
    item = OrderItem(
        product_name=product_name,
        dosage=_parse_dosage(dosage_match),
        flavor=flavor,
        quantity=_extract_quantity(line),
        is_gift=any(word in low for word in ("gift", "free", "bonus", "бонус", "подарок", "на пробу")),
    )
    return item, product_name


def fallback_parse_order_text(text: str) -> ExtractedOrder:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and _line_looks_like_shop(lines[0]):
        shop_name = lines[0]
        item_lines = lines[1:]
    else:
        shop_name = _extract_inline_shop_name(text)
        item_lines = lines if "\n" in text else [text]

    total_match = re.search(r"(?:total|итого|сумма)\s*:?\s*([0-9][0-9,\s]*(?:\.\d+)?)", text, flags=re.I)
    total_amount = _to_number(total_match.group(1)) if total_match else None
    payment = _normalize_payment(text)

    items: list[OrderItem] = []
    current_product: str | None = None
    skip_words = ("total", "итого", "сумма", "paid", "оплачено", "delivered", "waiting", "credit", "track", "shipped")
    for line in item_lines:
        low = line.lower()
        if any(word in low for word in skip_words) and not re.search(r"(mg|мг|g|гр|г)(?![a-zа-я])", low):
            continue
        item, current_product = _parse_item_line(line, current_product)
        if item:
            items.append(item)

    return ExtractedOrder(
        shop_name=shop_name,
        items=items,
        suggested_payment_method=payment,
        total_amount=total_amount,
    )


def _is_bad_shop_name(shop_name: str | None, raw_text: str) -> bool:
    if not shop_name:
        return False
    compact_shop = " ".join(shop_name.split())
    compact_raw = " ".join(raw_text.split())
    return "\n" in shop_name or len(compact_shop) > 80 or compact_shop == compact_raw


def _merge_with_fallback(extracted: ExtractedOrder, fallback: ExtractedOrder, raw_text: str) -> ExtractedOrder:
    if _is_bad_shop_name(extracted.shop_name, raw_text):
        extracted.shop_name = None
    if not extracted.items and fallback.items:
        extracted.items = fallback.items
    if extracted.shop_name is None and fallback.shop_name and not _is_bad_shop_name(fallback.shop_name, raw_text):
        extracted.shop_name = fallback.shop_name
    if extracted.suggested_payment_method is None:
        extracted.suggested_payment_method = fallback.suggested_payment_method
    if extracted.total_amount is None:
        extracted.total_amount = fallback.total_amount
    for item in extracted.items:
        item.product_name = _standardize_product_name(item.product_name)
    return extracted


async def parse_order_text(text: str) -> dict:
    settings = get_settings()
    api_key = os.getenv("GEMINI_API_KEY")
    fallback = fallback_parse_order_text(text)
    if not api_key:
        return fallback.model_dump(mode="json")

    client = genai.Client(api_key=api_key)
    try:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=ExtractedOrder,
            ),
        )
        parsed = response.parsed
        if parsed is None:
            parsed = ExtractedOrder.model_validate(json.loads(response.text))
        elif not isinstance(parsed, ExtractedOrder):
            parsed = ExtractedOrder.model_validate(parsed)
        return _merge_with_fallback(parsed, fallback, text).model_dump(mode="json")
    except Exception:
        return fallback.model_dump(mode="json")
