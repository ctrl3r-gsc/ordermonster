import re
from datetime import datetime
from decimal import Decimal
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import DeliveryStatus, Order, OrderItem, OrderPayment, PaymentMethod, PaymentStatus, Product, Shop


UTC_TZ = ZoneInfo("UTC")
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")


SHOP_PREFIX_RE = re.compile(
    r"^\s*(?:🛒\s*)?(?:"
    r"here['’]?s\s+your\s+order\s+for|"
    r"обновл[её]нный\s+заказ\s+для|"
    r"заказ\s+для|"
    r"ptt\s+customer|"
    r"new\s+order|"
    r"order\s+for|"
    r"order|"
    r"shop|"
    r"store"
    r")\s*(?:[:\-–—]\s*)*",
    flags=re.I,
)
EMOJI_RE = re.compile(
    "["
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001FAFF"
    "]+"
)
SHOP_SPECIAL_CHARS_RE = re.compile(r"[^\w\s&.'’]", flags=re.UNICODE)
def make_sku(name: str, dosage: int | None, flavor: str | None) -> str:
    raw = "-".join(str(part) for part in (name, dosage or "na", flavor or "plain"))
    sku = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return sku[:250] or "unknown"


def decimal_money(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def bangkok_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC_TZ)
    else:
        value = value.astimezone(UTC_TZ)
    return value.astimezone(BANGKOK_TZ)


def format_order_datetime(value: datetime | None) -> str:
    local_value = bangkok_datetime(value)
    if local_value is None:
        return "unknown"
    return local_value.strftime("%d.%m (%H:%M)")


def format_dashboard_datetime(value: datetime | None) -> str:
    local_value = bangkok_datetime(value)
    if local_value is None:
        return "--.-- --:--"
    return local_value.strftime("%d.%m %H:%M")


def display_order_number(order: Order) -> int:
    return order.display_number or order.id


def item_unit_price(item: OrderItem) -> Decimal:
    if item.is_gift:
        return Decimal("0.00")
    return decimal_money(item.price_per_unit)


def item_subtotal(item: OrderItem) -> Decimal:
    return Decimal(item.quantity) * item_unit_price(item)


def calculate_order_total(order: Order) -> Decimal:
    return sum((item_subtotal(item) for item in order.items), Decimal("0.00")).quantize(Decimal("0.01"))


def calculated_unit_price(product: Product, shop: Shop, is_gift: bool = False) -> Decimal:
    if is_gift:
        return Decimal("0.00")
    base_price = decimal_money(product.price)
    modifier = decimal_money(shop.price_modifier)
    return max(Decimal("0.00"), base_price + modifier).quantize(Decimal("0.01"))


def sanitize_shop_name(name: str | None) -> str:
    if not name:
        return ""
    clean_name = str(name).strip()
    previous_name = None
    while previous_name != clean_name:
        previous_name = clean_name
        clean_name = SHOP_PREFIX_RE.sub("", clean_name).strip()
    clean_name = EMOJI_RE.sub("", clean_name)
    clean_name = SHOP_SPECIAL_CHARS_RE.sub(" ", clean_name)
    clean_name = re.sub(r"\s+", " ", clean_name)
    return clean_name.strip(" :-–—.,'’").upper()


def clean_contact_value(value: str | None) -> str | None:
    clean = (value or "").strip()
    if not clean or clean.lower() in {"not specified", "unknown", "none", "null"}:
        return None
    return clean


def strip_phone_from_address(address: str | None, phone_number: str | None) -> str | None:
    clean_address = clean_contact_value(address)
    clean_phone = clean_contact_value(phone_number)
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


def sterilize_address(raw_address, phone_number):
    if not raw_address:
        return ""

    # 1. Remove the exact phone number first
    clean_addr = raw_address.replace(phone_number or "", "").strip()

    # 2. Hard-strip any sequence of 9-11 digits (phones, long numbers)
    # This acts as a final firewall against any numbers slipping through
    clean_addr = re.sub(r"\d{9,11}", "", clean_addr)

    # 3. Collapse extra newlines left by the removal
    clean_addr = re.sub(r"\n+", "\n", clean_addr).strip()

    return clean_addr


async def get_or_create_shop(session: AsyncSession, name: str, address: str | None = None, phone_number: str | None = None) -> Shop:
    clean_name = (name or "").upper().strip()
    clean_name = sanitize_shop_name(clean_name)
    if not clean_name:
        raise ValueError("Shop name cannot be empty")
    address = clean_contact_value(address)
    phone_number = clean_contact_value(phone_number)
    shop = await session.scalar(select(Shop).where(func.lower(Shop.name) == clean_name.lower()))
    if shop:
        if address and address != shop.address:
            shop.address = address
        if phone_number and phone_number != shop.phone_number:
            shop.phone_number = phone_number
        return shop
    shop = Shop(name=clean_name, address=address, phone_number=phone_number, price_modifier=Decimal("0.00"))
    session.add(shop)
    await session.flush()
    return shop


async def top_shops(session: AsyncSession, limit: int = 10, search: str | None = None) -> list[Shop]:
    last_order_at = func.max(Order.created_at).label("last_order_at")
    stmt: Select[tuple[Shop]] = select(Shop).outerjoin(Order, Order.shop_id == Shop.id).group_by(Shop.id)
    if search:
        stmt = stmt.where(Shop.name.ilike(f"%{search}%"))
    stmt = stmt.order_by(last_order_at.desc().nulls_last(), Shop.name.asc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def all_shops(session: AsyncSession) -> list[Shop]:
    last_order_at = func.max(Order.created_at).label("last_order_at")
    stmt = select(Shop).outerjoin(Order, Order.shop_id == Shop.id).group_by(Shop.id)
    stmt = stmt.order_by(last_order_at.desc().nulls_last(), Shop.name.asc())
    return list((await session.scalars(stmt)).all())


def normalize_shop_name(name: str | None) -> str:
    clean_name = sanitize_shop_name(name)
    if not clean_name:
        return ""
    translit = str.maketrans(
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
    return re.sub(r"[^a-z0-9]+", "", clean_name.lower().translate(translit))


def match_existing_shop_name(shop_name: str | None, shops: list[Shop]) -> Shop | None:
    normalized = normalize_shop_name(shop_name)
    if not normalized:
        return None

    best_shop: Shop | None = None
    best_score = 0.0
    for shop in shops:
        candidate = normalize_shop_name(shop.name)
        if not candidate:
            continue
        if normalized == candidate:
            return shop
        if normalized in candidate or candidate in normalized:
            score = 0.92
        else:
            score = SequenceMatcher(None, normalized, candidate).ratio()
        if score > best_score:
            best_score = score
            best_shop = shop
    return best_shop if best_score >= 0.76 else None


async def get_or_create_product(
    session: AsyncSession,
    name: str,
    dosage: int | None,
    flavor: str | None,
    price: Decimal | int | str = Decimal("0"),
    potency_type: str | None = None,
    is_active: bool = True,
    force_update: bool = True,
) -> Product:
    sku = make_sku(name, dosage, flavor)
    base_price = Decimal(str(price))
    product = await session.scalar(select(Product).where(Product.sku == sku))
    if product:
        if base_price > 0 and (force_update or decimal_money(product.price) == 0):
            product.price = base_price
        if force_update:
            product.potency_type = potency_type or product.potency_type
            product.is_active = is_active
        return product
    product = Product(
        name=name.strip(),
        dosage=dosage,
        flavor=flavor,
        potency_type=potency_type,
        sku=sku,
        price=base_price,
        is_active=is_active,
    )
    session.add(product)
    await session.flush()
    return product


async def find_product_for_item(
    session: AsyncSession,
    name: str,
    dosage: int | None,
    flavor: str | None,
) -> Product | None:
    clean_name = (name or "").strip()
    if not clean_name:
        return None
    stmt = select(Product).where(Product.name.ilike(clean_name), Product.is_active.is_(True))
    if dosage is not None:
        stmt = stmt.where(Product.dosage == dosage)
    return await session.scalar(stmt.limit(1))


def parsed_shop_contact(parsed: dict) -> tuple[str | None, str | None]:
    phone_number = clean_contact_value(parsed.get("phone_number"))
    return clean_contact_value(sterilize_address(parsed.get("address"), phone_number)), phone_number


async def shop_from_parsed(session: AsyncSession, parsed: dict, fallback_shop: Shop | None = None) -> Shop:
    shop_name = (parsed.get("shop_name") or "").upper().strip()
    address, phone_number = parsed_shop_contact(parsed)
    if shop_name:
        return await get_or_create_shop(session, shop_name, address=address, phone_number=phone_number)
    if fallback_shop is None:
        raise ValueError("Shop name cannot be empty")
    if address and address != fallback_shop.address:
        fallback_shop.address = address
    if phone_number and phone_number != fallback_shop.phone_number:
        fallback_shop.phone_number = phone_number
    await session.flush()
    return fallback_shop


async def create_order_from_parsed(session: AsyncSession, parsed: dict, shop: Shop, user_id: int) -> Order:
    order_data = dict(parsed)
    extracted_phone = order_data.get("phone_number")
    raw_address = order_data.get("address")
    order_data["address"] = sterilize_address(raw_address, extracted_phone)
    parsed = order_data
    shop = await shop_from_parsed(session, parsed, fallback_shop=shop)
    await backfill_missing_order_display_numbers(session)
    order = Order(
        display_number=await next_order_display_number(session),
        shop_id=shop.id,
        user_id=user_id,
        total_amount=Decimal("0.00"),
    )
    session.add(order)
    await session.flush()
    calculated_total = Decimal("0.00")
    for item in parsed.get("items", []):
        quantity = int(item.get("quantity") or 1)
        is_gift = bool(item.get("is_gift"))
        parsed_product_name = item.get("product_name") or "unknown"
        product = await find_product_for_item(
            session, parsed_product_name, item.get("dosage"), item.get("flavor")
        )
        product_missing = product is None
        if product is None:
            product = await get_or_create_product(
                session,
                f"UNKNOWN: {parsed_product_name}",
                item.get("dosage"),
                item.get("flavor"),
                price=Decimal("0.00"),
                is_active=False,
                force_update=False,
            )
        unit_price = Decimal("0.00") if product_missing else calculated_unit_price(product, shop, is_gift)
        if not is_gift:
            calculated_total += Decimal(quantity) * unit_price
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                price_per_unit=unit_price,
                is_gift=is_gift,
            )
        )
    order.total_amount = calculated_total.quantize(Decimal("0.01"))
    await session.flush()
    await session.commit()
    return await get_order(session, order.id)


async def next_order_display_number(session: AsyncSession) -> int:
    current_max = await session.scalar(select(func.max(Order.display_number)))
    return int(current_max or 0) + 1


async def backfill_missing_order_display_numbers(session: AsyncSession) -> None:
    orders = list((await session.scalars(select(Order).where(Order.display_number.is_(None)))).all())
    for order in orders:
        order.display_number = order.id
    if orders:
        await session.flush()


async def recalculate_order_total(session: AsyncSession, order: Order) -> Order:
    order.total_amount = calculate_order_total(order)
    refresh_payment_status(order)
    await session.flush()
    return await get_order(session, order.id)


async def update_item_unit_price(
    session: AsyncSession,
    order_id: int,
    item_id: int,
    price_per_unit: Decimal,
) -> Order:
    order = await get_order(session, order_id)
    target = next((item for item in order.items if item.id == item_id), None)
    if target is None:
        raise ValueError(f"Order item {item_id} not found in order {order_id}")
    target.price_per_unit = max(Decimal("0.00"), decimal_money(price_per_unit))
    target.is_gift = target.price_per_unit == 0
    await session.flush()
    order = await get_order(session, order_id)
    return await recalculate_order_total(session, order)


async def get_order(session: AsyncSession, order_id: int) -> Order:
    order = await session.scalar(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.shop), selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.payments))
    )
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.display_number is None:
        order.display_number = order.id
        await session.flush()
    return order


def paid_amount(order: Order) -> Decimal:
    return sum((payment.amount for payment in order.payments), Decimal("0"))


def remaining_amount(order: Order) -> Decimal:
    total = order.total_amount or Decimal("0")
    return max(total - paid_amount(order), Decimal("0"))


def refresh_payment_status(order: Order) -> None:
    total = order.total_amount or Decimal("0")
    paid = paid_amount(order)
    if total > 0 and paid >= total:
        order.payment_status = PaymentStatus.paid
    elif paid > 0:
        order.payment_status = PaymentStatus.partially_paid
    else:
        order.payment_status = PaymentStatus.unpaid


async def add_payment(session: AsyncSession, order: Order, method: str, amount: Decimal) -> Order:
    session.add(OrderPayment(order_id=order.id, payment_method=PaymentMethod(method), amount=amount))
    await session.flush()
    order = await get_order(session, order.id)
    refresh_payment_status(order)
    await session.flush()
    return await get_order(session, order.id)


async def dashboard_orders(session: AsyncSession, page: int = 0, limit: int = 10) -> list[Order]:
    await backfill_missing_order_display_numbers(session)
    page = max(page, 0)
    limit = max(limit, 1)
    return list(
        (
            await session.scalars(
                select(Order)
                .options(selectinload(Order.shop))
                .order_by(Order.display_number.desc())
                .limit(limit)
                .offset(page * limit)
            )
        ).all()
    )


async def dashboard_has_next_page(session: AsyncSession, page: int = 0, limit: int = 10) -> bool:
    page = max(page, 0)
    limit = max(limit, 1)
    total_orders = await session.scalar(select(func.count(Order.id)))
    return (total_orders or 0) > (page + 1) * limit
