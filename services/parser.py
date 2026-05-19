import json
import os
import re
from difflib import SequenceMatcher
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, field_validator

from config import get_settings
from services.orders import sanitize_shop_name


CatalogItem = Literal[
    "Ultimate Gummies Mango 100mg",
    "Ultimate Gummies Green Apple 250mg",
    "Ultimate Gummies Strawberry 500mg",
    "X-Hash Gummies Pineapple 150mg",
    "X-Hash Gummies Blackcurrant 350mg",
    "X-Hash Gummies Watermelon 600mg",
    "Rosin Gummies Green Apple 250mg",
    "Breakfast Cookies 100mg",
    "Brownie 100mg",
    "Cookies 100mg",
    "Magic Gummies 1g 1000mg",
    "Magic Gummies 2g 2000mg",
]

CATALOG_ITEM_DETAILS: dict[str, tuple[str, int]] = {
    "Ultimate Gummies Mango 100mg": ("Ultimate Gummies Mango", 100),
    "Ultimate Gummies Green Apple 250mg": ("Ultimate Gummies Green Apple", 250),
    "Ultimate Gummies Strawberry 500mg": ("Ultimate Gummies Strawberry", 500),
    "X-Hash Gummies Pineapple 150mg": ("X-Hash Gummies Pineapple", 150),
    "X-Hash Gummies Blackcurrant 350mg": ("X-Hash Gummies Blackcurrant", 350),
    "X-Hash Gummies Watermelon 600mg": ("X-Hash Gummies Watermelon", 600),
    "Rosin Gummies Green Apple 250mg": ("Rosin Gummies Green Apple", 250),
    "Breakfast Cookies 100mg": ("Breakfast Cookies", 100),
    "Brownie 100mg": ("Brownie", 100),
    "Cookies 100mg": ("Cookies", 100),
    "Magic Gummies 1g 1000mg": ("Magic Gummies 1g", 1000),
    "Magic Gummies 2g 2000mg": ("Magic Gummies 2g", 2000),
}


class OrderItem(BaseModel):
    product_name: CatalogItem
    dosage: int | None = None
    flavor: str | None = None
    quantity: int = Field(ge=1)
    is_gift: bool = False

    @field_validator("quantity", mode="before")
    @classmethod
    def ensure_minimum_quantity(cls, v):
        if v is None:
            return 1
        try:
            val = int(v)
            return val if val >= 1 else 1
        except (ValueError, TypeError):
            return 1


class ExtractedOrder(BaseModel):
    shop_name: str | None = None
    address: str | None = None
    phone_number: str | None = None
    items: list[OrderItem] = Field(default_factory=list)
    suggested_payment_method: Literal["cash", "transaction", "crypto"] | None = None
    total_amount: float | None = None


ExtractedOrderItem = OrderItem
OrderExtractionModel = ExtractedOrder


SYSTEM_INSTRUCTION = (
    "You are a precise CRM assistant for a confectionery and bar business order tracking system. "
    "Your job is to parse messy, unstructured text messages into a strict JSON schema.\n\n"
    "CRITICAL RULES:\n"
    "Return clean JSON with these top-level fields: `shop_name`, `address`, `phone_number`, `items`, "
    "`suggested_payment_method`, and `total_amount`.\n"
    "1. `shop_name`: Extract ONLY the specific name of the shop/client (e.g., 'шаман', 'SHAMAN', 'TAI MA TON'). "
    "NEVER copy the whole text here! If no shop name is mentioned in the text, set it to null.\n"
    "   `shop_name` MUST contain only the clean raw establishment brand in UPPERCASE. Never include labels, prefixes, punctuation, emojis, or order phrases such as 'Shop:', 'Store:', 'New Order', 'Order for', 'Order:', 'Заказ для', or 'Обновлённый заказ для'.\n"
    "2. `phone_number`: Extract the mobile/phone number ONLY from explicit contact information.\n"
    "   - Look for standard Thai formats: '+66...', '09...', '08...', '06...' or other 10-digit numbers.\n"
    "   - Look for keywords near numbers: 'Mobile:', 'Tel:', 'Phone:', 'contact', 'mobile', 'телефон', 'номер'.\n"
    "   - If no phone number is found, set to null or empty string.\n"
    "3. `address` (if provided in text): Extract the full delivery address text into the 'address' field, including condo/apartment name, room number, street, district, or any specific location markers.\n"
    "   - STRICT EXTRACTION RULE: If the message says 'New order for SHOP. Address: Condo Room 105. Mobile: +66...', "
    "you MUST extract 'Condo Room 105' into `address` and '+66...' into `phone_number`.\n"
    "   - If a phone number (for example, starting with +66, 09, 08, 06) is written inside or next to the address, extract those digits into the 'phone_number' field, AND keep the location text intact inside the 'address' field.\n"
    "   - The `address` field might just be a URL (for example a Google Maps link). If the user provides a link as the location, extract the exact URL string into the `address` field. Do not leave it empty.\n"
    "   - NEVER leave the address as 'not specified' or null if a physical location is mentioned in the text.\n"
    "4. `items`: Extract every single ordered product into this array.\n"
    "   - `product_name`: Map to an exact product from the database catalog below, not a broad category.\n"
    "     AVAILABLE DATABASE CATALOG PRODUCTS:\n"
    "     1. Ultimate Gummies Mango 100mg\n"
    "     2. Ultimate Gummies Green Apple 250mg\n"
    "     3. Ultimate Gummies Strawberry 500mg\n"
    "     4. X-Hash Gummies Pineapple 150mg\n"
    "     5. X-Hash Gummies Blackcurrant 350mg\n"
    "     6. X-Hash Gummies Watermelon 600mg\n"
    "     7. Rosin Gummies Green Apple 250mg\n"
    "     8. Breakfast Cookies 100mg\n"
    "     9. Brownie 100mg\n"
    "     10. Cookies 100mg\n"
    "     11. Magic Gummies 1g 1000mg\n"
    "     12. Magic Gummies 2g 2000mg\n"
    "     You MUST map the user's requested item to one of these exact catalog items. Pay strict attention to the brand (for example X-Hash vs Ultimate vs Rosin vs Magic) and the flavor (for example Watermelon vs Mango vs Strawberry vs Green Apple). Do not hallucinate or guess items that are not on this list.\n"
    "     You must classify the user's item strictly into one of the allowed Enum/Literal values. Use brand and flavor context to make the best match, even if the user forgets the exact dosage.\n"
    "     In JSON, `product_name` MUST be one exact full catalog label from the schema, including dosage. Example: 'x-hash watermelon gummies' -> {\"product_name\": \"X-Hash Gummies Watermelon 600mg\", \"dosage\": 600, \"flavor\": \"watermelon\"}.\n"
    "     If the user mentions a brand/flavor but omits the dosage (mg), map it to the most logical matching item in the provided catalog based on the brand and flavor they DID specify, but NEVER change the flavor or brand just to find a match.\n"
    "     Users will make typos when writing product names (e.g., 'guumies' instead of 'gummies'). "
    "You must logically map these typos to the correct exact catalog product when the brand/flavor/dosage evidence points to one.\n"
    "   - `dosage`: Extract ONLY the integer number of milligrams (e.g., '500мг' -> 500).\n"
    "     If the user orders 'gummies' (мармелад), 'brownie' (брауни), or 'cookie' (печенье) WITHOUT specifying milligrams, you MUST automatically set `dosage` to 100. Never leave it null or skip the item.\n"
    "   - `flavor`: Extract the flavor string (e.g., 'клубника', 'strawberry'). If not mentioned -> null.\n"
    "   - `quantity`: Extract the exact integer count. Quantity can appear before or after the product name. If the user writes '10 pcs gummies 500mg', the item quantity MUST be exactly 10, not the default 1.\n"
    "   - `is_gift`: Set to true ONLY if words like 'бонус', 'подарок', 'на пробу', 'gift' are near the item.\n"
    "5. `suggested_payment_method`: Strictly 'cash', 'transaction', 'crypto', or null.\n"
    "6. `total_amount`: Extract the numeric total price if explicitly provided at the end (e.g., '3000' or '3,000').\n\n"
    "EXAMPLES OF CORRECT PARSING:\n\n"
    "Input: 'бро привет, запиши нам 10 пачек гамми 500мг клубника в шаман, оплата налик'\n"
    "Output:\n"
    "{\n"
    "  \"shop_name\": \"ШАМАН\",\n"
    "  \"items\": [\n"
    "    {\"product_name\": \"Ultimate Gummies Strawberry\", \"dosage\": 500, \"flavor\": \"клубника\", \"quantity\": 10, \"is_gift\": false}\n"
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
    "}\n\n"
    "Input: 'gummies 250mg 5 500mg 15 brownie 20 testshop'\n"
    "Output:\n"
    "{\n"
    "  \"shop_name\": \"TESTSHOP\",\n"
    "  \"items\": [\n"
    "    {\"product_name\": \"Ultimate Gummies Green Apple\", \"dosage\": 250, \"flavor\": null, \"quantity\": 5, \"is_gift\": false},\n"
    "    {\"product_name\": \"Ultimate Gummies Strawberry\", \"dosage\": 500, \"flavor\": null, \"quantity\": 15, \"is_gift\": false},\n"
    "    {\"product_name\": \"Brownie\", \"dosage\": 100, \"flavor\": null, \"quantity\": 20, \"is_gift\": false}\n"
    "  ],\n"
    "  \"suggested_payment_method\": null,\n"
    "  \"total_amount\": null\n"
    "}\n"
    "\n"
    "Input: 'gummies 500mg 30 pcs\\nbrownie 15\\ngummies 30'\n"
    "Output:\n"
    "{\n"
    "  \"shop_name\": null,\n"
    "  \"items\": [\n"
    "    {\"product_name\": \"Ultimate Gummies Strawberry\", \"dosage\": 500, \"flavor\": null, \"quantity\": 30, \"is_gift\": false},\n"
    "    {\"product_name\": \"Brownie\", \"dosage\": 100, \"flavor\": null, \"quantity\": 15, \"is_gift\": false},\n"
    "    {\"product_name\": \"Ultimate Gummies Mango\", \"dosage\": 100, \"flavor\": null, \"quantity\": 30, \"is_gift\": false}\n"
    "  ],\n"
    "  \"suggested_payment_method\": null,\n"
    "  \"total_amount\": null\n"
    "}\n"
)


PRODUCT_ALIASES = {
    "magic gummies": "Magic Gummies",
    "magic gummy": "Magic Gummies",
    "magic": "Magic Gummies",
    "guumies": "Gummies",
    "gumies": "Gummies",
    "gummys": "Gummies",
    "gummy": "Gummies",
    "gummies": "Gummies",
    "gumi": "Gummies",
    "gummie": "Gummies",
    "гамми": "Gummies",
    "гамме": "Gummies",
    "broni": "Brownie",
    "browni": "Brownie",
    "brownies": "Brownie",
    "brownie": "Brownie",
    "брауни": "Brownie",
    "cookie": "Cookie",
    "cookies": "Cookie",
    "cooki": "Cookie",
    "cokie": "Cookie",
    "печенье": "Cookie",
    "cbd drops": "Drops",
    "cbd drop": "Drops",
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

GIFT_WORDS = ("gift", "free", "bonus", "бонус", "подарок", "на пробу")


def _build_system_instruction(existing_shops: list[str] | None = None) -> str:
    if not existing_shops:
        return SYSTEM_INSTRUCTION
    shop_list = ", ".join(f"'{shop}'" for shop in existing_shops if shop)
    return (
        f"{SYSTEM_INSTRUCTION}\n\n"
        "VALID EXISTING SHOPS:\n"
        f"Here is a list of valid existing shops: {shop_list}. "
        "If the input text contains a misspelled, shorthand, or lowercase version of one of these shops "
        "(e.g., 'шман' or 'shaman' for 'SHAMAN'), you MUST automatically correct it and return the EXACT "
        "name from this list in the `shop_name` field. If none of these shops are mentioned, keep the extracted "
        "new shop name or null according to the schema rules."
    )


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


def _translit_ru(value: str) -> str:
    return value.translate(
        str.maketrans(
            {
                "а": "a",
                "б": "b",
                "в": "v",
                "г": "g",
                "д": "d",
                "е": "e",
                "ё": "e",
                "ж": "zh",
                "з": "z",
                "и": "i",
                "й": "y",
                "к": "k",
                "л": "l",
                "м": "m",
                "н": "n",
                "о": "o",
                "п": "p",
                "р": "r",
                "с": "s",
                "т": "t",
                "у": "u",
                "ф": "f",
                "х": "h",
                "ц": "ts",
                "ч": "ch",
                "ш": "sh",
                "щ": "sch",
                "ы": "y",
                "э": "e",
                "ю": "yu",
                "я": "ya",
                "ь": "",
                "ъ": "",
            }
        )
    )


def _normalize_shop_name(value: str | None) -> str:
    value = sanitize_shop_name(value)
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "", _translit_ru(value.lower()))


def _best_existing_shop_match(candidate: str | None, existing_shops: list[str] | None) -> str | None:
    normalized = _normalize_shop_name(candidate)
    if not normalized or not existing_shops:
        return None

    best_name: str | None = None
    best_score = 0.0
    for shop in existing_shops:
        shop_normalized = _normalize_shop_name(shop)
        if not shop_normalized:
            continue
        if normalized == shop_normalized:
            return shop
        score = 0.92 if normalized in shop_normalized or shop_normalized in normalized else SequenceMatcher(None, normalized, shop_normalized).ratio()
        if score > best_score:
            best_score = score
            best_name = shop
    return best_name if best_score >= 0.76 else None


def _best_shop_mentioned_in_text(text: str, existing_shops: list[str] | None) -> str | None:
    if not existing_shops:
        return None
    normalized_text = _normalize_shop_name(text)
    for shop in existing_shops:
        shop_normalized = _normalize_shop_name(shop)
        if shop_normalized and shop_normalized in normalized_text:
            return shop

    tokens = [token for token in re.split(r"[^a-zа-я0-9]+", text.lower()) if len(token) >= 3]
    best_name: str | None = None
    best_score = 0.0
    for shop in existing_shops:
        shop_normalized = _normalize_shop_name(shop)
        if not shop_normalized:
            continue
        for token in tokens:
            score = SequenceMatcher(None, _normalize_shop_name(token), shop_normalized).ratio()
            if score > best_score:
                best_score = score
                best_name = shop
    return best_name if best_score >= 0.82 else None


def _standardize_product_name(value: str) -> str:
    low = value.lower().strip()
    for alias, normalized in PRODUCT_ALIASES.items():
        if alias in low:
            return normalized
    compact_tokens = re.findall(r"[a-zа-яё]+", low, flags=re.I)
    for token in compact_tokens:
        best_alias = max(PRODUCT_ALIASES, key=lambda alias: SequenceMatcher(None, token, alias).ratio())
        if SequenceMatcher(None, token, best_alias).ratio() >= 0.78:
            return PRODUCT_ALIASES[best_alias]
    return value.strip() or "unknown"


def _parse_dosage(match: re.Match[str] | None) -> int | None:
    if not match:
        return None
    amount = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    return int(amount * 1000) if unit in {"g", "г", "гр"} else int(amount)


def _default_dosage(product_name: str, dosage: int | None) -> int | None:
    if dosage is not None:
        return dosage
    if _standardize_product_name(product_name) in {"Gummies", "Brownie", "Cookie"}:
        return 100
    return None


def _catalog_label_for_item(product_name: str, dosage: int | None, flavor: str | None = None) -> CatalogItem:
    low_name = product_name.lower()
    low_flavor = (flavor or "").lower()
    search = f"{low_name} {low_flavor}"

    if "magic" in search:
        return "Magic Gummies 2g 2000mg" if dosage == 2000 or "2g" in search else "Magic Gummies 1g 1000mg"
    if "breakfast" in search:
        return "Breakfast Cookies 100mg"
    if "brownie" in search or "брауни" in search:
        return "Brownie 100mg"
    if "cookie" in search or "печенье" in search:
        return "Cookies 100mg"
    if "x-hash" in search or "x hash" in search:
        if "watermelon" in search or dosage == 600:
            return "X-Hash Gummies Watermelon 600mg"
        if "blackcurrant" in search or "black currant" in search or dosage == 350:
            return "X-Hash Gummies Blackcurrant 350mg"
        return "X-Hash Gummies Pineapple 150mg"
    if "rosin" in search:
        return "Rosin Gummies Green Apple 250mg"
    if "watermelon" in search:
        return "X-Hash Gummies Watermelon 600mg"
    if "pineapple" in search:
        return "X-Hash Gummies Pineapple 150mg"
    if "blackcurrant" in search or "black currant" in search:
        return "X-Hash Gummies Blackcurrant 350mg"
    if "strawberry" in search or "клубник" in search or dosage == 500:
        return "Ultimate Gummies Strawberry 500mg"
    if "green apple" in search or "яблок" in search or dosage == 250:
        return "Ultimate Gummies Green Apple 250mg"
    return "Ultimate Gummies Mango 100mg"


def _normalize_catalog_item(item: OrderItem) -> None:
    product_name = str(item.product_name)
    db_name, catalog_dosage = CATALOG_ITEM_DETAILS[product_name]
    item.product_name = db_name
    item.dosage = catalog_dosage


def _normalize_extracted_order(order: ExtractedOrder) -> ExtractedOrder:
    for item in order.items:
        _normalize_catalog_item(item)
    return order


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


def _extract_phone_number(text: str) -> str | None:
    thai_phone_pattern = r"(?:\+66[\s().-]*|0)(?:[689]\d)[\s().-]*\d{3}[\s().-]*\d{4}"
    keyword_match = re.search(
        rf"(?:mobile|phone|tel|contact|телефон|номер)\s*:?\s*({thai_phone_pattern})",
        text,
        flags=re.I,
    )
    if keyword_match:
        return re.sub(r"\s+", " ", keyword_match.group(1)).strip(" .,-")
    phone_match = re.search(thai_phone_pattern, text)
    if phone_match:
        return re.sub(r"\s+", " ", phone_match.group(0)).strip(" .,-")
    return None


def _extract_address(text: str) -> str | None:
    url_match = re.search(r"https?://\S+", text, flags=re.I)
    if url_match and (
        any(word in text.lower() for word in ("address", "addr", "location", "map", "maps", "адрес"))
        or any(word in url_match.group(0).lower() for word in ("maps", "goo.gl", "google"))
    ):
        return url_match.group(0).strip(" .,)")
    address_match = re.search(
        r"(?:address|addr|location|адрес)\s*:?\s*(.+?)(?=(?:\bmobile\b|\bphone\b|\btel\b|\bcontact\b|телефон|номер|$))",
        text,
        flags=re.I | re.S,
    )
    if not address_match:
        return None
    address = " ".join(address_match.group(1).split()).strip(" .,-")
    return address or None


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
    without_dosage = re.sub(r"\d+(?:[\.,]\d+)?\s*(?:mg|мг|g|гр|г)(?![a-zа-я])", "", low)
    bare_quantity = re.search(r"\b(\d+)\b", without_dosage)
    if bare_quantity and any(alias in low for alias in PRODUCT_ALIASES):
        return int(bare_quantity.group(1))
    return 1


def _has_quantity_signal(line: str) -> bool:
    low = line.lower()
    explicit_quantity = any(
        re.search(pattern, low, flags=re.I)
        for pattern in (
            r"\d+\s*(?:пачек|пачки|packs?|pcs?|pieces?|шт|штук|уп)",
            r"x\s*\d+",
            r"\d+\s*x",
        )
    )
    if explicit_quantity:
        return True
    without_dosage = re.sub(r"\d+(?:[\.,]\d+)?\s*(?:mg|мг|g|гр|г)(?![a-zа-я])", "", low)
    return bool(re.search(r"\b\d+\b", without_dosage) and any(alias in low for alias in PRODUCT_ALIASES))


def _extract_product_name(line: str, current_product: str | None) -> str:
    low = line.lower()
    for alias in PRODUCT_ALIASES:
        if alias in low:
            return PRODUCT_ALIASES[alias]
    standardized = _standardize_product_name(line)
    if standardized != line.strip() and standardized != "unknown":
        return standardized
    return current_product or "unknown"


def _product_alias_pattern() -> str:
    aliases = sorted((re.escape(alias) for alias in PRODUCT_ALIASES), key=len, reverse=True)
    return r"(?:^|\s)(" + "|".join(aliases) + r")\b"


def _parse_dense_inline_items(text: str) -> tuple[list[OrderItem], str | None]:
    low = text.lower()
    matches = list(re.finditer(_product_alias_pattern(), low, flags=re.I))
    if not matches:
        return [], None

    items: list[OrderItem] = []
    consumed_until = 0
    for index, match in enumerate(matches):
        alias = match.group(1)
        product_name = PRODUCT_ALIASES.get(alias.lower(), _standardize_product_name(alias))
        segment_start = match.end()
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        prefix_start = matches[index - 1].end() if index > 0 else 0
        prefix_low = text[prefix_start:match.start()].lower()
        segment = text[segment_start:segment_end]
        segment_low = segment.lower()
        segment_consumed_until = 0
        prefix_quantity = None
        prefix_quantity_match = re.search(
            r"(\d+)\s*(?:packs?|pcs?|pieces?|шт|штук|уп|пачек|пачки)\s*$",
            prefix_low,
            flags=re.I,
        )
        if prefix_quantity_match:
            prefix_quantity = int(prefix_quantity_match.group(1))

        dosage_quantity_matches = list(
            re.finditer(
                r"(\d+(?:[\.,]\d+)?)\s*(mg|мг|g|гр|г)(?![a-zа-я])\s*(?:x\s*)?(\d+)?\s*(?:packs?|pcs?|pieces?|шт|штук|уп)?",
                segment_low,
                flags=re.I,
            )
        )
        if dosage_quantity_matches:
            for dosage_quantity in dosage_quantity_matches:
                dosage = _parse_dosage(dosage_quantity)
                flavor = next((flavor for flavor in FLAVORS if flavor in segment_low), None)
                items.append(
                    OrderItem(
                        product_name=_catalog_label_for_item(product_name, dosage, flavor),
                        dosage=dosage,
                        flavor=flavor,
                        quantity=int(dosage_quantity.group(3)) if dosage_quantity.group(3) else prefix_quantity or 1,
                        is_gift=any(word in segment_low for word in GIFT_WORDS),
                    )
                )
                segment_consumed_until = max(segment_consumed_until, dosage_quantity.end())
        else:
            quantity_match = re.search(r"\b(\d+)\b", segment_low)
            if quantity_match:
                dosage = _default_dosage(product_name, None)
                flavor = next((flavor for flavor in FLAVORS if flavor in segment_low), None)
                items.append(
                    OrderItem(
                        product_name=_catalog_label_for_item(product_name, dosage, flavor),
                        dosage=dosage,
                        flavor=flavor,
                        quantity=int(quantity_match.group(1)),
                        is_gift=any(word in segment_low for word in GIFT_WORDS),
                    )
                )
                segment_consumed_until = quantity_match.end()

        consumed_until = max(consumed_until, segment_start + segment_consumed_until)

    trailing_shop = None
    if consumed_until:
        tail = text[consumed_until:].strip(" ,.;:-")
        tail = re.sub(
            r"\b(?:paid|cash|bank|transfer|transaction|card|налик|наличные|перевод|карта|банк)\b.*$",
            "",
            tail,
            flags=re.I,
        ).strip(" ,.;:-")
        tail = re.sub(r"^(?:for|to)\s+(?:shop\s+)?", "", tail, flags=re.I).strip(" ,.;:-")
        if tail and not any(alias in tail.lower() for alias in PRODUCT_ALIASES):
            trailing_shop = tail
    return items, trailing_shop


def _parse_item_line(line: str, current_product: str | None) -> tuple[OrderItem | None, str | None]:
    low = line.lower()
    dosage_match = re.search(r"(\d+(?:[\.,]\d+)?)\s*(mg|мг|g|гр|г)(?![a-zа-я])", low)
    has_quantity = _has_quantity_signal(line)
    if not dosage_match and not has_quantity:
        clean_product = re.sub(r"[^a-zA-Zа-яА-Я0-9 -]", "", line).strip()
        return None, _standardize_product_name(clean_product) if clean_product else current_product

    product_name = _extract_product_name(line, current_product)
    dosage = _default_dosage(product_name, _parse_dosage(dosage_match))
    flavor = next((flavor for flavor in FLAVORS if flavor in low), None)
    item = OrderItem(
        product_name=_catalog_label_for_item(product_name, dosage, flavor),
        dosage=dosage,
        flavor=flavor,
        quantity=_extract_quantity(line),
        is_gift=any(word in low for word in GIFT_WORDS),
    )
    return item, product_name


def fallback_parse_order_text(text: str, existing_shops: list[str] | None = None) -> ExtractedOrder:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    dense_items, dense_shop = _parse_dense_inline_items(text) if "\n" not in text else ([], None)
    if lines and _line_looks_like_shop(lines[0]):
        shop_name = lines[0]
        item_lines = lines[1:]
    else:
        shop_name = dense_shop or _extract_inline_shop_name(text)
        item_lines = lines if "\n" in text else [text]

    total_match = re.search(r"(?:total|итого|сумма)\s*:?\s*([0-9][0-9,\s]*(?:\.\d+)?)", text, flags=re.I)
    total_amount = _to_number(total_match.group(1)) if total_match else None
    payment = _normalize_payment(text)

    items: list[OrderItem] = dense_items.copy()
    current_product: str | None = None
    if not dense_items:
        skip_words = ("total", "итого", "сумма", "paid", "оплачено", "delivered", "waiting", "credit", "track", "shipped")
        for line in item_lines:
            low = line.lower()
            if any(word in low for word in skip_words) and not re.search(r"(mg|мг|g|гр|г)(?![a-zа-я])", low):
                continue
            item, current_product = _parse_item_line(line, current_product)
            if item:
                items.append(item)

    resolved_shop_name = (
        _best_existing_shop_match(shop_name, existing_shops)
        or _best_shop_mentioned_in_text(text, existing_shops)
        or shop_name
    )

    return ExtractedOrder(
        shop_name=sanitize_shop_name(resolved_shop_name),
        address=_extract_address(text),
        phone_number=_extract_phone_number(text),
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


def _clean_optional_text(value: str | None) -> str | None:
    clean = (value or "").strip()
    if not clean or clean.lower() in {"not specified", "unknown", "none", "null"}:
        return None
    return clean


def _merge_with_fallback(
    extracted: ExtractedOrder,
    fallback: ExtractedOrder,
    raw_text: str,
    existing_shops: list[str] | None = None,
) -> ExtractedOrder:
    extracted.shop_name = sanitize_shop_name(extracted.shop_name)
    fallback.shop_name = sanitize_shop_name(fallback.shop_name)
    if _is_bad_shop_name(extracted.shop_name, raw_text):
        extracted.shop_name = None
    matched_shop = _best_existing_shop_match(extracted.shop_name, existing_shops) or _best_shop_mentioned_in_text(
        raw_text, existing_shops
    )
    if matched_shop:
        extracted.shop_name = sanitize_shop_name(matched_shop)
    if not extracted.items and fallback.items:
        extracted.items = fallback.items
    if extracted.shop_name is None and fallback.shop_name and not _is_bad_shop_name(fallback.shop_name, raw_text):
        extracted.shop_name = sanitize_shop_name(fallback.shop_name)
    extracted.address = _clean_optional_text(extracted.address) or _clean_optional_text(fallback.address)
    extracted.phone_number = _clean_optional_text(extracted.phone_number) or _clean_optional_text(fallback.phone_number)
    if extracted.suggested_payment_method is None:
        extracted.suggested_payment_method = fallback.suggested_payment_method
    if extracted.total_amount is None:
        extracted.total_amount = fallback.total_amount
    return _normalize_extracted_order(extracted)


async def parse_order_text(text: str, existing_shops: list[str] | None = None) -> dict:
    settings = get_settings()
    api_key = os.getenv("GEMINI_API_KEY")
    fallback = fallback_parse_order_text(text, existing_shops)
    if not api_key:
        return _normalize_extracted_order(fallback).model_dump(mode="json")

    client = genai.Client(api_key=api_key)
    try:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=_build_system_instruction(existing_shops),
                response_mime_type="application/json",
                response_schema=ExtractedOrder,
            ),
        )
        parsed = response.parsed
        if parsed is None:
            parsed = ExtractedOrder.model_validate(json.loads(response.text))
        elif not isinstance(parsed, ExtractedOrder):
            parsed = ExtractedOrder.model_validate(parsed)
        return _merge_with_fallback(parsed, fallback, text, existing_shops).model_dump(mode="json")
    except Exception:
        return _normalize_extracted_order(fallback).model_dump(mode="json")
