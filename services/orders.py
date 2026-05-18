import re
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import DeliveryStatus, Order, OrderItem, OrderPayment, PaymentMethod, PaymentStatus, Product, Shop


def make_sku(name: str, dosage: int | None, flavor: str | None) -> str:
    raw = "-".join(str(part) for part in (name, dosage or "na", flavor or "plain"))
    sku = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return sku[:250] or "unknown"


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
        if force_update and base_price > 0:
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


async def create_order_from_parsed(session: AsyncSession, parsed: dict, shop: Shop, user_id: int) -> Order:
    order = Order(shop_id=shop.id, user_id=user_id, total_amount=parsed.get("total_amount"))
    session.add(order)
    await session.flush()
    for item in parsed.get("items", []):
        product = await get_or_create_product(
            session,
            item.get("product_name") or "unknown",
            item.get("dosage"),
            item.get("flavor"),
        )
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=int(item.get("quantity") or 1),
                is_gift=bool(item.get("is_gift")),
            )
        )
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
