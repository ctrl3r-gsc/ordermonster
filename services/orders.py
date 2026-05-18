import re
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import DeliveryStatus, Order, OrderItem, OrderPayment, PaymentMethod, PaymentStatus, Product, Shop


def make_sku(name: str, dosage: int | None, flavor: str | None) -> str:
    raw = "-".join(str(part) for part in (name, dosage or "na", flavor or "plain"))
    sku = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return sku[:250] or "unknown"


def decimal_money(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def item_unit_price(item: OrderItem) -> Decimal:
    if item.is_gift:
        return Decimal("0.00")
    return decimal_money(item.product.price)


def item_subtotal(item: OrderItem) -> Decimal:
    return Decimal(item.quantity) * item_unit_price(item)


def calculate_order_total(order: Order) -> Decimal:
    return sum((item_subtotal(item) for item in order.items), Decimal("0.00"))


async def get_or_create_shop(session: AsyncSession, name: str, address: str | None = None) -> Shop:
    clean_name = name.strip()
    shop = await session.scalar(select(Shop).where(func.lower(Shop.name) == clean_name.lower()))
    if shop:
        if address and not shop.address:
            shop.address = address
        return shop
    shop = Shop(name=clean_name, address=address)
    session.add(shop)
    await session.flush()
    return shop


async def top_shops(session: AsyncSession, limit: int = 10, search: str | None = None) -> list[Shop]:
    stmt: Select[tuple[Shop]] = select(Shop)
    if search:
        stmt = stmt.where(Shop.name.ilike(f"%{search}%"))
    stmt = stmt.order_by(Shop.created_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def all_shops(session: AsyncSession) -> list[Shop]:
    return list((await session.scalars(select(Shop).order_by(Shop.name.asc()))).all())


def normalize_shop_name(name: str | None) -> str:
    if not name:
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
    return re.sub(r"[^a-z0-9]+", "", name.lower().translate(translit))


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
    price_up_to_10: Decimal | int | str | None = None,
    price_10_to_30: Decimal | int | str | None = None,
    price_30_plus: Decimal | int | str | None = None,
    is_active: bool = True,
    force_update: bool = True,
) -> Product:
    sku = make_sku(name, dosage, flavor)
    base_price = Decimal(str(price_up_to_10 if price_up_to_10 is not None else price))
    product = await session.scalar(select(Product).where(Product.sku == sku))
    if product:
        if base_price > 0 and (force_update or decimal_money(product.price) == 0):
            product.price = base_price
        if force_update:
            product.potency_type = potency_type or product.potency_type
            product.price_up_to_10 = Decimal(str(price_up_to_10)) if price_up_to_10 is not None else product.price_up_to_10
            product.price_10_to_30 = Decimal(str(price_10_to_30)) if price_10_to_30 is not None else product.price_10_to_30
            product.price_30_plus = Decimal(str(price_30_plus)) if price_30_plus is not None else product.price_30_plus
            product.is_active = is_active
        return product
    product = Product(
        name=name.strip().lower(),
        dosage=dosage,
        flavor=flavor,
        potency_type=potency_type,
        sku=sku,
        price=base_price,
        price_up_to_10=Decimal(str(price_up_to_10)) if price_up_to_10 is not None else base_price,
        price_10_to_30=Decimal(str(price_10_to_30)) if price_10_to_30 is not None else None,
        price_30_plus=Decimal(str(price_30_plus)) if price_30_plus is not None else None,
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
    clean_name = (name or "").strip().lower()
    clean_flavor = flavor.strip().lower() if flavor else None
    sku = make_sku(clean_name, dosage, flavor)

    product = await session.scalar(select(Product).where(Product.sku == sku, Product.is_active.is_(True)))
    if product:
        return product

    stmt = select(Product).where(Product.is_active.is_(True))
    if dosage is not None:
        stmt = stmt.where(Product.dosage == dosage)
    if clean_flavor:
        stmt = stmt.where(func.lower(Product.flavor) == clean_flavor)
    exact = await session.scalar(stmt.where(func.lower(Product.name) == clean_name).limit(1))
    if exact:
        return exact

    name_words = [word for word in re.split(r"[^a-zа-я0-9]+", clean_name) if len(word) >= 4]
    fuzzy_terms = [clean_name, *name_words]
    fuzzy_filters = [func.lower(Product.name).ilike(f"%{term}%") for term in fuzzy_terms if term]
    if fuzzy_filters:
        fuzzy_stmt = select(Product).where(Product.is_active.is_(True), or_(*fuzzy_filters))
        if dosage is not None:
            fuzzy_stmt = fuzzy_stmt.where(Product.dosage == dosage)
        if clean_flavor:
            fuzzy_stmt = fuzzy_stmt.where(
                or_(Product.flavor.is_(None), func.lower(Product.flavor) == clean_flavor)
            )
        product = await session.scalar(fuzzy_stmt.order_by(Product.price.desc()).limit(1))
        if product:
            return product

    return None


async def create_order_from_parsed(session: AsyncSession, parsed: dict, shop: Shop, user_id: int) -> Order:
    order = Order(shop_id=shop.id, user_id=user_id, total_amount=Decimal("0.00"))
    session.add(order)
    await session.flush()
    calculated_total = Decimal("0.00")
    for item in parsed.get("items", []):
        quantity = int(item.get("quantity") or 1)
        is_gift = bool(item.get("is_gift"))
        product = await find_product_for_item(
            session, item.get("product_name") or "unknown", item.get("dosage"), item.get("flavor")
        )
        if product is None:
            product = await get_or_create_product(
                session,
                item.get("product_name") or "unknown",
                item.get("dosage"),
                item.get("flavor"),
                force_update=False,
            )
        if not is_gift:
            calculated_total += Decimal(quantity) * decimal_money(product.price)
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                is_gift=is_gift,
            )
        )
    order.total_amount = calculated_total.quantize(Decimal("0.01"))
    await session.flush()
    return await get_order(session, order.id)


async def get_order(session: AsyncSession, order_id: int) -> Order:
    order = await session.scalar(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.shop), selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.payments))
    )
    if order is None:
        raise ValueError(f"Order {order_id} not found")
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


async def dashboard_orders(session: AsyncSession) -> list[Order]:
    return list(
        (
            await session.scalars(
                select(Order)
                .where((Order.delivery_status != DeliveryStatus.delivered) | (Order.payment_status != PaymentStatus.paid))
                .options(selectinload(Order.shop))
                .order_by(Order.updated_at.desc())
                .limit(50)
            )
        ).all()
    )
