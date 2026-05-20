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
    "ULTIMATE GUMMIES MANGO 100mg",
    "ULTIMATE GUMMIES GREEN APPLE 250mg",
    "ULTIMATE GUMMIES STRAWBERRY 500mg",
    "X-HASH GUMMIES PINEAPPLE 150mg",
    "X-HASH GUMMIES BLACKCURRANT 350mg",
    "X-HASH GUMMIES WATERMELON 600mg",
    "ROSIN GUMMIES GREEN APPLE 250mg",
    "BREAKFAST COOKIES 100mg",
    "BROWNIE 100mg THC",
    "COOKIES 100mg THC",
    "MAGIC GUMMIES 1g",
    "MAGIC GUMMIES 2g",
    "HASH",
]

CATALOG_ITEM_DETAILS: dict[str, tuple[str, int | None]] = {
    "ULTIMATE GUMMIES MANGO 100mg": ("ULTIMATE GUMMIES MANGO 100mg", 100),
    "ULTIMATE GUMMIES GREEN APPLE 250mg": ("ULTIMATE GUMMIES GREEN APPLE 250mg", 250),
    "ULTIMATE GUMMIES STRAWBERRY 500mg": ("ULTIMATE GUMMIES STRAWBERRY 500mg", 500),
    "X-HASH GUMMIES PINEAPPLE 150mg": ("X-HASH GUMMIES PINEAPPLE 150mg", 150),
    "X-HASH GUMMIES BLACKCURRANT 350mg": ("X-HASH GUMMIES BLACKCURRANT 350mg", 350),
    "X-HASH GUMMIES WATERMELON 600mg": ("X-HASH GUMMIES WATERMELON 600mg", 600),
    "ROSIN GUMMIES GREEN APPLE 250mg": ("ROSIN GUMMIES GREEN APPLE 250mg", 250),
    "BREAKFAST COOKIES 100mg": ("BREAKFAST COOKIES 100mg", 100),
    "BROWNIE 100mg THC": ("BROWNIE 100mg THC", 100),
    "COOKIES 100mg THC": ("COOKIES 100mg THC", 100),
    "MAGIC GUMMIES 1g": ("MAGIC GUMMIES 1g", 1000),
    "MAGIC GUMMIES 2g": ("MAGIC GUMMIES 2g", 2000),
    "HASH": ("HASH", None),
}


class OrderItem(BaseModel):
    product_id: int | None = None
    product_name: str | None = None
    raw_product_text: str | None = None
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
    payment_method: Literal["cash", "transaction", "crypto"] | None = None
    suggested_payment_method: Literal["cash", "transaction", "crypto"] | None = None
    total_amount: float | None = None
    unresolved_products: list[dict] = Field(default_factory=list)


ExtractedOrderItem = OrderItem
OrderExtractionModel = ExtractedOrder

QUANTITY_TOKEN_RE = re.compile(
    r"^\s*\d+\s*(?:packs?|pcs?|pieces?|pc|pack|шт|штук|уп|пачек|пачки)\s*$",
    flags=re.I,
)
QUANTITY_RE = re.compile(
    r"\b\d+\s*(?:packs?|pcs?|pieces?|pc|pack|шт|штук|уп|пачек|пачки)\b",
    flags=re.I,
)
GENERIC_PRODUCT_WORDS = {"gummies", "gummy", "guumies", "gumies", "gummys", "gumi", "gummie"}
GENERIC_FAMILY_WORDS = GENERIC_PRODUCT_WORDS | {"cookie", "cookies", "brownie", "brownies", "hash"}
MATCH_STOPWORDS = GENERIC_FAMILY_WORDS | {
    "mg",
    "g",
    "pc",
    "pcs",
    "pack",
    "packs",
    "piece",
    "pieces",
    "shop",
    "for",
    "to",
}


def _normalize_match_text(value: str | None) -> str:
    value = (value or "").lower()
    value = re.sub(r"(\d+)\s*(mg|мг)", r"\1mg", value)
    value = re.sub(r"(\d+)\s*(g|гр|г)\b", r"\1g", value)
    value = value.replace("black currant", "blackcurrant")
    return re.sub(r"[^a-z0-9а-яё]+", " ", value).strip()


def _strip_quantity_tokens(value: str | None) -> str:
    clean = QUANTITY_RE.sub(" ", value or "")
    clean = re.sub(r"\bx\s*\d+\b|\b\d+\s*x\b", " ", clean, flags=re.I)
    return re.sub(r"\s+", " ", clean).strip()


def _is_quantity_only(value: str | None) -> bool:
    return bool(QUANTITY_TOKEN_RE.fullmatch(value or ""))


def _clean_shop_candidate(value: str | None) -> str | None:
    clean = (value or "").strip(" -:,.")
    if not clean or _is_quantity_only(clean):
        return None
    low = clean.lower()
    if re.fullmatch(r"(?:product\s+)?line\s+[a-z0-9]+", low, flags=re.I):
        return None
    if low in {"product", "product line", "line", "name"}:
        return None
    return clean


def _sanitize_parser_shop_name(value: str | None) -> str | None:
    clean = _clean_shop_candidate(value)
    if clean is None:
        return None
    if re.match(r"^shop\s+\w", clean, flags=re.I):
        sanitized = re.sub(r"[^a-zA-Z0-9а-яА-Я&.'’\s_-]+", " ", clean)
        sanitized = re.sub(r"\s+", " ", sanitized).strip(" :-–—.,'’")
        return sanitized.upper() or None
    sanitized = sanitize_shop_name(clean)
    return sanitized or None


def _has_explicit_dosage(value: str | None) -> bool:
    if not value:
        return False
    return bool(re.search(r"\b\d+(?:[\.,]\d+)?\s*(?:mg|мг|g|гр|г)\b", value.lower(), flags=re.I))


def _word_tokens(value: str | None) -> set[str]:
    return set(re.findall(r"[a-zа-яё]+", _normalize_match_text(value), flags=re.I))


def _catalog_prompt(catalog_products: list[dict] | None) -> str:
    if not catalog_products:
        return ""
    lines = ["\nACTIVE PRODUCTS. You may only choose product_id from this list:"]
    for product in catalog_products:
        aliases = ", ".join(product.get("aliases") or [])
        lines.append(
            f"- product_id={product['product_id']}; name={product['name']}; "
            f"dosage={product.get('dosage')}; price={product.get('price')}; aliases=[{aliases}]"
        )
    lines.append(
        "For every parsed item return product_id from the list and quantity only. If no listed product fits, "
        "or the text only says a generic family like gummies without dosage/flavor/line, set product_id to null. "
        "Never invent products, prices, ids, or aliases. Never put quantity tokens such as 1pc, 2 packs, or 10pcs "
        "into shop_name."
    )
    return "\n".join(lines)


def _catalog_by_id(catalog_products: list[dict] | None) -> dict[int, dict]:
    return {int(product["product_id"]): product for product in catalog_products or [] if product.get("product_id") is not None}


def _item_search_text(item: OrderItem, raw_text: str) -> str:
    bits = [item.raw_product_text, item.flavor]
    if not item.raw_product_text:
        bits.append(item.product_name)
    if not any(bits):
        bits.append(raw_text)
    return " ".join(str(bit) for bit in bits if bit)


def _candidate_strings(product: dict) -> list[str]:
    dosage = product.get("dosage")
    dosage_bits = []
    if dosage is not None:
        dosage_bits = [f"{dosage}", f"{dosage}mg"]
        if int(dosage) >= 1000 and int(dosage) % 1000 == 0:
            dosage_bits.append(f"{int(dosage) // 1000}g")
    values = [product.get("name", ""), *(product.get("aliases") or [])]
    return [f"{value} {' '.join(dosage_bits)}" for value in values if value]


def _family_keywords(product: dict) -> set[str]:
    keywords: set[str] = set()
    for value in [product.get("name", ""), *(product.get("aliases") or [])]:
        keywords.update(token for token in _word_tokens(value) if token not in MATCH_STOPWORDS)
    return keywords


def _query_line_keywords(query: str, catalog: list[dict]) -> set[str]:
    query_tokens = _word_tokens(query)
    known_line_keywords: set[str] = set()
    for product in catalog:
        known_line_keywords.update(_family_keywords(product))
    return query_tokens & known_line_keywords


def _product_matches_line_keywords(product: dict, line_keywords: set[str]) -> bool:
    return not line_keywords or bool(_family_keywords(product) & line_keywords)


def _generic_product_query(query: str) -> bool:
    tokens = _word_tokens(query)
    meaningful = {
        token
        for token in tokens
        if token not in MATCH_STOPWORDS and not token.isdigit() and not re.fullmatch(r"\d+(?:mg|g)", token)
    }
    return bool(tokens & GENERIC_PRODUCT_WORDS) and not meaningful


def _extract_dosages(text: str) -> set[int]:
    values: set[int] = set()
    for match in re.finditer(r"(\d+(?:[\.,]\d+)?)\s*(mg|мг|g|гр|г)\b", text.lower(), flags=re.I):
        amount = float(match.group(1).replace(",", "."))
        unit = match.group(2).lower()
        values.add(int(amount * 1000) if unit in {"g", "гр", "г"} else int(amount))
    for match in re.finditer(r"\b(100|150|250|350|500|600)\b", text):
        values.add(int(match.group(1)))
    return values


def _resolve_item_product(item: OrderItem, catalog_products: list[dict] | None, raw_text: str) -> tuple[OrderItem, list[int]]:
    catalog = catalog_products or []
    product_by_id = _catalog_by_id(catalog)
    query_source = _item_search_text(item, raw_text)
    query = _normalize_match_text(_strip_quantity_tokens(query_source))
    line_keywords = _query_line_keywords(query, catalog)
    if item.product_id and item.product_id in product_by_id:
        product = product_by_id[item.product_id]
        if not _product_matches_line_keywords(product, line_keywords):
            item.product_id = None
            return item, [
                int(candidate["product_id"])
                for candidate in catalog
                if _product_matches_line_keywords(candidate, line_keywords)
            ][:5]
        item.product_name = product.get("name")
        item.dosage = product.get("dosage")
        return item, []

    if not query:
        return item, []

    query_dosages = _extract_dosages(query)
    explicit_dosage = _has_explicit_dosage(query_source) or bool(query_dosages)
    candidates = [product for product in catalog if _product_matches_line_keywords(product, line_keywords)]
    if not candidates:
        candidates = catalog
    if line_keywords:
        exact_line_candidates = [
            product for product in candidates if line_keywords <= _family_keywords(product)
        ]
        if len(exact_line_candidates) == 1:
            candidates = exact_line_candidates
        elif len(exact_line_candidates) > 1 and not explicit_dosage:
            return item, [int(product["product_id"]) for product in exact_line_candidates[:5]]
    family_candidates = [
        product
        for product in candidates
        if any(word in _normalize_match_text(product.get("name")) for word in GENERIC_FAMILY_WORDS & _word_tokens(query))
    ]
    if family_candidates:
        candidates = family_candidates

    if not explicit_dosage and len(candidates) > 1 and _generic_product_query(query):
        return item, [int(product["product_id"]) for product in candidates[:5]]

    scored: list[tuple[float, dict]] = []
    for product in candidates:
        best = 0.0
        for candidate in _candidate_strings(product):
            normalized_candidate = _normalize_match_text(candidate)
            aliases = [_normalize_match_text(alias) for alias in product.get("aliases") or []]
            exact_alias = query in aliases or any(alias and alias in query for alias in aliases)
            if exact_alias:
                best = max(best, 1.0)
            if normalized_candidate and normalized_candidate in query:
                best = max(best, 0.95)
            best = max(best, SequenceMatcher(None, query, normalized_candidate).ratio() * 0.72)
        product_dosage = product.get("dosage")
        if query_dosages and product_dosage in query_dosages:
            best += 0.25
        elif query_dosages and product_dosage is not None:
            best -= 0.20
        name_norm = _normalize_match_text(product.get("name"))
        if "gummies" in query and "gummies" in name_norm:
            best += 0.08
        if "cookie" in query and "cookie" in name_norm:
            best += 0.08
        if "brownie" in query and "brownie" in name_norm:
            best += 0.08
        scored.append((best, product))

    scored.sort(key=lambda row: row[0], reverse=True)
    similar = [int(product["product_id"]) for score, product in scored[:5] if score >= 0.45]
    if not scored:
        return item, similar
    best_score, best_product = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    if best_score >= 0.86 and best_score - second_score >= 0.08:
        item.product_id = int(best_product["product_id"])
        item.product_name = best_product.get("name")
        item.dosage = best_product.get("dosage")
        return item, []
    return item, similar


def resolve_products_for_order(order: ExtractedOrder, catalog_products: list[dict] | None, raw_text: str) -> ExtractedOrder:
    unresolved: list[dict] = []
    for index, item in enumerate(order.items):
        resolved, similar = _resolve_item_product(item, catalog_products, raw_text)
        if not resolved.product_id:
            unresolved.append(
                {
                    "item_index": index,
                    "quantity": resolved.quantity,
                    "raw_product_text": resolved.raw_product_text or resolved.product_name,
                    "dosage": resolved.dosage,
                    "similar_product_ids": similar,
                }
            )
    if unresolved:
        order.unresolved_products = unresolved
    return order


SYSTEM_INSTRUCTION = (
    "You are a precise CRM assistant for a confectionery and bar business order tracking system. "
    "Your job is to parse messy, unstructured text messages into a strict JSON schema.\n\n"
    "CRITICAL RULES:\n"
    "Return clean JSON with these top-level fields: `shop_name`, `address`, `phone_number`, `items`, "
    "`payment_method`, `suggested_payment_method`, and `total_amount`.\n"
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
    "   - You must explicitly STRIP the phone number digits out of the final 'address' string. The address should only contain the physical location text, without duplicating the contact number.\n"
    "   - The `address` field might just be a URL (for example a Google Maps link). If the user provides a link as the location, extract the exact URL string into the `address` field. Do not leave it empty.\n"
    "   - NEVER leave the address as 'not specified' or null if a physical location is mentioned in the text.\n"
    "4. `items`: Extract every single ordered product into this array.\n"
    "   - `product_name`: Map to an exact product from the database catalog below, not a broad category.\n"
    "     AVAILABLE DATABASE CATALOG PRODUCTS:\n"
    "     1. ULTIMATE GUMMIES MANGO 100mg\n"
    "     2. ULTIMATE GUMMIES GREEN APPLE 250mg\n"
    "     3. ULTIMATE GUMMIES STRAWBERRY 500mg\n"
    "     4. X-HASH GUMMIES PINEAPPLE 150mg\n"
    "     5. X-HASH GUMMIES BLACKCURRANT 350mg\n"
    "     6. X-HASH GUMMIES WATERMELON 600mg\n"
    "     7. ROSIN GUMMIES GREEN APPLE 250mg\n"
    "     8. BREAKFAST COOKIES 100mg\n"
    "     9. BROWNIE 100mg THC\n"
    "     10. COOKIES 100mg THC\n"
    "     11. MAGIC GUMMIES 1g\n"
    "     12. MAGIC GUMMIES 2g\n"
    "     13. HASH\n"
    "     You MUST map the user's requested item to one of these exact catalog items. Pay strict attention to the brand (for example X-Hash vs Ultimate vs Rosin vs Magic) and the flavor (for example Watermelon vs Mango vs Strawberry vs Green Apple). Do not hallucinate or guess items that are not on this list.\n"
    "     You must classify the user's item strictly into one of the allowed Enum/Literal values. Use brand and flavor context to make the best match, even if the user forgets the exact dosage.\n"
    "     STRICT DOSAGE PRIORITY: When mapping items to the allowed Enum catalog, DOSAGE (e.g., 150mg, 100mg, 500mg) is the HIGHEST priority matching criteria. If the user explicitly writes '150mg', you MUST select the catalog item that matches '150mg'. NEVER substitute a different dosage if the correct dosage exists in the catalog.\n"
    "     In JSON, `product_name` MUST be one exact full catalog label from the schema, including dosage. Example: 'x-hash watermelon gummies' -> {\"product_name\": \"X-HASH GUMMIES WATERMELON 600mg\", \"dosage\": 600, \"flavor\": \"watermelon\"}.\n"
    "     If the user mentions a brand/flavor but omits the dosage (mg), map it to the most logical matching item in the provided catalog based on the brand and flavor they DID specify, but NEVER change the flavor or brand just to find a match.\n"
    "     Users will make typos when writing product names (e.g., 'guumies' instead of 'gummies'). "
    "You must logically map these typos to the correct exact catalog product when the brand/flavor/dosage evidence points to one.\n"
    "   - `dosage`: Extract ONLY the integer number of milligrams (e.g., '500мг' -> 500).\n"
    "     If the user orders 'gummies' (мармелад), 'brownie' (брауни), or 'cookie' (печенье) WITHOUT specifying milligrams, you MUST automatically set `dosage` to 100. Never leave it null or skip the item.\n"
    "   - `flavor`: Extract the flavor string (e.g., 'клубника', 'strawberry'). If not mentioned -> null.\n"
    "   - `quantity`: Extract the exact integer count. Quantity can appear before or after the product name. If the user writes '10 pcs gummies 500mg', the item quantity MUST be exactly 10, not the default 1.\n"
    "   - `is_gift`: Set to true ONLY if words like 'бонус', 'подарок', 'на пробу', 'gift' are near the item.\n"
    "5. `payment_method` / `suggested_payment_method`: Strictly 'cash', 'transaction', 'crypto', or null.\n"
    "6. `total_amount`: Extract the numeric total price if explicitly provided at the end (e.g., '3000' or '3,000').\n\n"
    "EXAMPLES OF CORRECT PARSING:\n\n"
    "Input: 'бро привет, запиши нам 10 пачек гамми 500мг клубника в шаман, оплата налик'\n"
    "Output:\n"
    "{\n"
    "  \"shop_name\": \"ШАМАН\",\n"
    "  \"items\": [\n"
    "    {\"product_name\": \"ULTIMATE GUMMIES STRAWBERRY 500mg\", \"dosage\": 500, \"flavor\": \"клубника\", \"quantity\": 10, \"is_gift\": false}\n"
    "  ],\n"
    "  \"suggested_payment_method\": \"cash\",\n"
    "  \"total_amount\": null\n"
    "}\n\n"
    "Input: 'TAI MA TON\\nbrownie\\n100mg x30 - 3,000\\n500mg x1 - gift\\ntotal: 3,000 thb paid cash'\n"
    "Output:\n"
    "{\n"
    "  \"shop_name\": \"TAI MA TON\",\n"
    "  \"items\": [\n"
    "    {\"product_name\": \"BROWNIE 100mg THC\", \"dosage\": 100, \"flavor\": null, \"quantity\": 30, \"is_gift\": false},\n"
    "    {\"product_name\": \"BROWNIE 100mg THC\", \"dosage\": 100, \"flavor\": null, \"quantity\": 1, \"is_gift\": true}\n"
    "  ],\n"
    "  \"suggested_payment_method\": \"cash\",\n"
    "  \"total_amount\": 3000\n"
    "}\n\n"
    "Input: 'gummies 250mg 5 500mg 15 brownie 20 testshop'\n"
    "Output:\n"
    "{\n"
    "  \"shop_name\": \"TESTSHOP\",\n"
    "  \"items\": [\n"
    "    {\"product_name\": \"ULTIMATE GUMMIES GREEN APPLE 250mg\", \"dosage\": 250, \"flavor\": null, \"quantity\": 5, \"is_gift\": false},\n"
    "    {\"product_name\": \"ULTIMATE GUMMIES STRAWBERRY 500mg\", \"dosage\": 500, \"flavor\": null, \"quantity\": 15, \"is_gift\": false},\n"
    "    {\"product_name\": \"BROWNIE 100mg THC\", \"dosage\": 100, \"flavor\": null, \"quantity\": 20, \"is_gift\": false}\n"
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
    "    {\"product_name\": \"ULTIMATE GUMMIES STRAWBERRY 500mg\", \"dosage\": 500, \"flavor\": null, \"quantity\": 30, \"is_gift\": false},\n"
    "    {\"product_name\": \"BROWNIE 100mg THC\", \"dosage\": 100, \"flavor\": null, \"quantity\": 15, \"is_gift\": false},\n"
    "    {\"product_name\": \"ULTIMATE GUMMIES MANGO 100mg\", \"dosage\": 100, \"flavor\": null, \"quantity\": 30, \"is_gift\": false}\n"
    "  ],\n"
    "  \"suggested_payment_method\": null,\n"
    "  \"total_amount\": null\n"
    "}\n"
)


PRODUCT_ALIASES = {
    "magic gummies": "Magic Gummies",
    "magic gummy": "Magic Gummies",
    "magic": "Magic Gummies",
    "x-hash gummies": "X-Hash Gummies",
    "x hash gummies": "X-Hash Gummies",
    "x-hash": "X-Hash Gummies",
    "x hash": "X-Hash Gummies",
    "hash": "Hash",
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


def _build_system_instruction(existing_shops: list[str] | None = None, catalog_products: list[dict] | None = None) -> str:
    instruction = f"{SYSTEM_INSTRUCTION}{_catalog_prompt(catalog_products)}"
    if not existing_shops:
        return instruction
    shop_list = ", ".join(f"'{shop}'" for shop in existing_shops if shop)
    return (
        f"{instruction}\n\n"
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
        return "MAGIC GUMMIES 2g" if dosage == 2000 or "2g" in search else "MAGIC GUMMIES 1g"
    if "breakfast" in search:
        return "BREAKFAST COOKIES 100mg"
    if "brownie" in search or "брауни" in search:
        return "BROWNIE 100mg THC"
    if "cookie" in search or "печенье" in search:
        return "COOKIES 100mg THC"
    if "x-hash" in search or "x hash" in search:
        if "watermelon" in search or dosage == 600:
            return "X-HASH GUMMIES WATERMELON 600mg"
        if "blackcurrant" in search or "black currant" in search or dosage == 350:
            return "X-HASH GUMMIES BLACKCURRANT 350mg"
        return "X-HASH GUMMIES PINEAPPLE 150mg"
    if "hash" in search and "gumm" not in search:
        return "HASH"
    if "rosin" in search:
        return "ROSIN GUMMIES GREEN APPLE 250mg"
    if "watermelon" in search:
        return "X-HASH GUMMIES WATERMELON 600mg"
    if "pineapple" in search:
        return "X-HASH GUMMIES PINEAPPLE 150mg"
    if "blackcurrant" in search or "black currant" in search:
        return "X-HASH GUMMIES BLACKCURRANT 350mg"
    if dosage == 150:
        return "X-HASH GUMMIES PINEAPPLE 150mg"
    if dosage == 350:
        return "X-HASH GUMMIES BLACKCURRANT 350mg"
    if dosage == 600:
        return "X-HASH GUMMIES WATERMELON 600mg"
    if "strawberry" in search or "клубник" in search or dosage == 500:
        return "ULTIMATE GUMMIES STRAWBERRY 500mg"
    if "green apple" in search or "яблок" in search or dosage == 250:
        return "ULTIMATE GUMMIES GREEN APPLE 250mg"
    return "ULTIMATE GUMMIES MANGO 100mg"


def _normalize_catalog_item(item: OrderItem) -> None:
    if item.product_id:
        return
    product_name = str(item.product_name or "")
    if product_name not in CATALOG_ITEM_DETAILS:
        return
    db_name, catalog_dosage = CATALOG_ITEM_DETAILS[product_name]
    item.product_name = db_name
    item.dosage = catalog_dosage


def _normalize_extracted_order(order: ExtractedOrder) -> ExtractedOrder:
    for item in order.items:
        _normalize_catalog_item(item)
    return order


def _extract_inline_shop_name(text: str) -> str | None:
    text = re.sub(r"https?://\S+|(?:maps\.app\.goo\.gl|goo\.gl|google\.com/maps)/?\S*", " ", text, flags=re.I)
    text = re.sub(r"\d{9,11}", " ", text)
    patterns = [
        r"(?:\bв|для|to|for)\s+([a-zA-Zа-яА-Я0-9 ._-]{2,40}?)(?:[,.;]|\s+(?:оплата|paid|налик|наличные|перевод|карта|банк|total|итого)|$)",
        r"(?:shop|client|клиент|магазин)\s*:?\s*([a-zA-Zа-яА-Я0-9 ._-]{2,40})(?:[,.;]|\n|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return _clean_shop_candidate(match.group(1))
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
    generic_match = re.search(r"\b\d{9,11}\b", text)
    if generic_match:
        return generic_match.group(0)
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
    if _is_quantity_only(line):
        return False
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
        segment_item_added = False
        prefix_quantity = None
        prefix_quantity_match = re.search(
            r"(\d+)\s*(?:packs?|pcs?|pieces?|шт|штук|уп|пачек|пачки)\s*$",
            prefix_low,
            flags=re.I,
        )
        if prefix_quantity_match:
            prefix_quantity = int(prefix_quantity_match.group(1))
        raw_context = f"{prefix_low} {alias} {segment}".strip()

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
                        raw_product_text=raw_context,
                        dosage=dosage,
                        flavor=flavor,
                        quantity=int(dosage_quantity.group(3)) if dosage_quantity.group(3) else prefix_quantity or 1,
                        is_gift=any(word in segment_low for word in GIFT_WORDS),
                    )
                )
                segment_item_added = True
                segment_consumed_until = max(segment_consumed_until, dosage_quantity.end())
        else:
            explicit_quantity_match = re.search(
                r"\b(\d+)\s*(?:packs?|pcs?|pieces?|pc|pack|шт|штук|уп|пачек|пачки)\b",
                segment_low,
                flags=re.I,
            )
            quantity_match = re.search(r"\b(\d+)\b", segment_low)
            if quantity_match or prefix_quantity:
                dosage = _default_dosage(product_name, None)
                if prefix_quantity:
                    quantity = prefix_quantity
                elif explicit_quantity_match:
                    quantity = int(explicit_quantity_match.group(1))
                else:
                    quantity = int(quantity_match.group(1))
                bare_values = [int(match.group(1)) for match in re.finditer(r"\b(100|150|250|350|500|600|1000|2000)\b", segment_low)]
                for bare_value in bare_values:
                    if bare_value != quantity:
                        dosage = bare_value
                        break
                if prefix_quantity and quantity_match and dosage == _default_dosage(product_name, None):
                    bare_value = int(quantity_match.group(1))
                    if bare_value in {100, 150, 250, 350, 500, 600, 1000, 2000}:
                        dosage = bare_value
                flavor = next((flavor for flavor in FLAVORS if flavor in segment_low), None)
                items.append(
                    OrderItem(
                        product_name=_catalog_label_for_item(product_name, dosage, flavor),
                        raw_product_text=raw_context,
                        dosage=dosage,
                        flavor=flavor,
                        quantity=quantity,
                        is_gift=any(word in segment_low for word in GIFT_WORDS),
                    )
                )
                segment_item_added = True
                segment_consumed_until = explicit_quantity_match.end() if explicit_quantity_match else quantity_match.end() if quantity_match else 0

        if segment_item_added:
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
        tail = re.sub(r"https?://\S+|(?:maps\.app\.goo\.gl|goo\.gl|google\.com/maps)/?\S*", " ", tail, flags=re.I)
        tail = re.sub(r"\d{9,11}", " ", tail)
        tail = QUANTITY_RE.sub(" ", tail)
        tail = re.sub(r"^(?:for|to)\s+(?:shop\s+)?", "", tail, flags=re.I).strip(" ,.;:-")
        tail = re.sub(r"\s+", " ", tail).strip(" ,.;:-")
        if tail and not any(alias in tail.lower() for alias in PRODUCT_ALIASES):
            trailing_shop = _clean_shop_candidate(tail)
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
    if dosage == 100 and not dosage_match:
        quantity = _extract_quantity(line)
        bare_dosages = [
            int(match.group(1))
            for match in re.finditer(r"\b(100|150|250|350|500|600|1000|2000)\b", low)
            if int(match.group(1)) != quantity
        ]
        if bare_dosages:
            dosage = bare_dosages[0]
    flavor = next((flavor for flavor in FLAVORS if flavor in low), None)
    item = OrderItem(
        product_name=_catalog_label_for_item(product_name, dosage, flavor),
        raw_product_text=line,
        dosage=dosage,
        flavor=flavor,
        quantity=_extract_quantity(line),
        is_gift=any(word in low for word in GIFT_WORDS),
    )
    return item, product_name


def _extract_trailing_shop_from_order_line(line: str) -> str | None:
    clean = re.sub(r"https?://\S+|(?:maps\.app\.goo\.gl|goo\.gl|google\.com/maps)/?\S*", " ", line, flags=re.I)
    clean = re.sub(r"\d+(?:[\.,]\d+)?\s*(?:mg|мг|g|гр|г)(?![a-zа-я])", " ", clean, flags=re.I)
    clean = re.sub(r"\d+\s*(?:packs?|pcs?|pieces?|шт|штук|уп|пачек|пачки)", " ", clean, flags=re.I)
    clean = re.sub(r"\b(?:x\s*\d+|\d+\s*x)\b", " ", clean, flags=re.I)
    clean = re.sub(r"\b\d+\b", " ", clean)
    for alias in PRODUCT_ALIASES:
        clean = re.sub(rf"\b{re.escape(alias)}\b", " ", clean, flags=re.I)
    clean = re.sub(r"\b(?:paid|cash|bank|transfer|transaction|card|total|итого)\b.*$", " ", clean, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip(" ,.;:-")
    return _clean_shop_candidate(clean)


def _split_item_line_candidates(line: str) -> list[str]:
    quantity_matches = list(
        re.finditer(
            r"\b\d+\s*(?:packs?|pcs?|pieces?|pc|pack|С€С‚|С€С‚СѓРє|СѓРї|РїР°С‡РµРє|РїР°С‡РєРё)\b",
            line,
            flags=re.I,
        )
    )
    if len(quantity_matches) <= 1:
        return [line]

    segments: list[str] = []
    start = 0
    for index, match in enumerate(quantity_matches):
        end = match.end()
        next_start = quantity_matches[index + 1].start() if index + 1 < len(quantity_matches) else None
        if next_start is not None and re.search(r"[a-zA-ZР°-СЏРђ-РЇ]", line[end:next_start]):
            segments.append(line[start:end].strip(" ,.;:-"))
            start = end
    segments.append(line[start:].strip(" ,.;:-"))
    return [segment for segment in segments if segment]


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
            for segment in _split_item_line_candidates(line):
                item, current_product = _parse_item_line(segment, current_product)
                if item:
                    items.append(item)
                    if (
                        not shop_name
                        and len(lines) == 1
                        and any(alias in segment.lower() for alias in PRODUCT_ALIASES)
                    ):
                        shop_name = _extract_trailing_shop_from_order_line(segment)

    resolved_shop_name = (
        _best_existing_shop_match(shop_name, existing_shops)
        or _best_shop_mentioned_in_text(text, existing_shops)
        or shop_name
    )

    return ExtractedOrder(
        shop_name=_sanitize_parser_shop_name(resolved_shop_name),
        address=_extract_address(text),
        phone_number=_extract_phone_number(text),
        items=items,
        payment_method=payment,
        suggested_payment_method=payment,
        total_amount=total_amount,
    )


def _is_bad_shop_name(shop_name: str | None, raw_text: str) -> bool:
    if not shop_name:
        return False
    if _clean_shop_candidate(shop_name) is None:
        return True
    compact_shop = " ".join(shop_name.split())
    compact_raw = " ".join(raw_text.split())
    return "\n" in shop_name or len(compact_shop) > 80 or compact_shop == compact_raw


def _clean_optional_text(value: str | None) -> str | None:
    clean = (value or "").strip()
    if not clean or clean.lower() in {"not specified", "unknown", "none", "null"}:
        return None
    return clean


def _strip_phone_from_address(address: str | None, phone_number: str | None) -> str | None:
    clean_address = _clean_optional_text(address)
    clean_phone = _clean_optional_text(phone_number)
    if not clean_address or not clean_phone:
        return clean_address
    stripped = clean_address.replace(clean_phone, " ")
    phone_digits = re.sub(r"\D+", "", clean_phone)
    if phone_digits:
        stripped = stripped.replace(phone_digits, " ")
    stripped = re.sub(r"[ \t]*\n[ \t]*", "\n", stripped)
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    stripped = re.sub(r"\n{2,}", "\n", stripped)
    return stripped.strip(" \t\r\n.,;:-") or None


def _finalize_extracted_order(order: ExtractedOrder) -> ExtractedOrder:
    order.phone_number = _clean_optional_text(order.phone_number)
    order.address = _strip_phone_from_address(order.address, order.phone_number)
    if order.payment_method is None:
        order.payment_method = order.suggested_payment_method
    if order.suggested_payment_method is None:
        order.suggested_payment_method = order.payment_method
    return _normalize_extracted_order(order)


def _merge_with_fallback(
    extracted: ExtractedOrder,
    fallback: ExtractedOrder,
    raw_text: str,
    existing_shops: list[str] | None = None,
) -> ExtractedOrder:
    extracted.shop_name = sanitize_shop_name(extracted.shop_name)
    extracted.shop_name = extracted.shop_name or None
    fallback.shop_name = _sanitize_parser_shop_name(fallback.shop_name)
    if _is_bad_shop_name(extracted.shop_name, raw_text):
        extracted.shop_name = None
    matched_shop = _best_existing_shop_match(extracted.shop_name, existing_shops) or _best_shop_mentioned_in_text(
        raw_text, existing_shops
    )
    if matched_shop:
        extracted.shop_name = sanitize_shop_name(matched_shop)
    if fallback.items and (not extracted.items or len(fallback.items) > len(extracted.items)):
        extracted.items = fallback.items
    if extracted.shop_name is None and fallback.shop_name and not _is_bad_shop_name(fallback.shop_name, raw_text):
        extracted.shop_name = sanitize_shop_name(fallback.shop_name)
    extracted.phone_number = _clean_optional_text(extracted.phone_number) or _clean_optional_text(fallback.phone_number)
    extracted.address = _clean_optional_text(extracted.address) or _clean_optional_text(fallback.address)
    extracted.address = _strip_phone_from_address(extracted.address, extracted.phone_number)
    if extracted.suggested_payment_method is None:
        extracted.suggested_payment_method = fallback.suggested_payment_method
    if extracted.payment_method is None:
        extracted.payment_method = extracted.suggested_payment_method or fallback.payment_method
    if extracted.total_amount is None:
        extracted.total_amount = fallback.total_amount
    return _finalize_extracted_order(extracted)


async def parse_order_text(
    text: str,
    existing_shops: list[str] | None = None,
    catalog_products: list[dict] | None = None,
) -> dict:
    settings = get_settings()
    api_key = os.getenv("GEMINI_API_KEY")
    fallback = fallback_parse_order_text(text, existing_shops)
    if not api_key:
        resolved = resolve_products_for_order(_finalize_extracted_order(fallback), catalog_products, text)
        return resolved.model_dump(mode="json")

    client = genai.Client(api_key=api_key)
    try:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=_build_system_instruction(existing_shops, catalog_products),
                response_mime_type="application/json",
                response_schema=ExtractedOrder,
            ),
        )
        parsed = response.parsed
        if parsed is None:
            parsed = ExtractedOrder.model_validate(json.loads(response.text))
        elif not isinstance(parsed, ExtractedOrder):
            parsed = ExtractedOrder.model_validate(parsed)
        merged = _merge_with_fallback(parsed, fallback, text, existing_shops)
        resolved = resolve_products_for_order(merged, catalog_products, text)
        return resolved.model_dump(mode="json")
    except Exception:
        resolved = resolve_products_for_order(_finalize_extracted_order(fallback), catalog_products, text)
        return resolved.model_dump(mode="json")
