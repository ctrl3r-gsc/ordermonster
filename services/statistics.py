from datetime import datetime, timedelta

from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryStatus, Order, OrderItem, OrderPayment, PaymentStatus, Product, Shop
from services.statistics_core import (
    BANGKOK_TZ,
    aggregate_debt_rows,
    aggregate_debt_shop_rows,
    aggregate_shop_sales_rows,
    aggregate_stats_rows,
    period_bounds,
)


MAX_ANALYTICS_ROWS = 20


def _apply_period_filter(stmt, start_at: datetime | None, end_at: datetime | None):
    if start_at is not None:
        stmt = stmt.where(Order.created_at >= start_at)
    if end_at is not None:
        stmt = stmt.where(Order.created_at <= end_at)
    return stmt


async def get_product_sales_stats(session: AsyncSession, period: str) -> dict:
    start_at, end_at = period_bounds(period)

    paid_orders_stmt = select(func.count(distinct(Order.id))).where(Order.payment_status == PaymentStatus.paid)
    paid_orders_stmt = _apply_period_filter(paid_orders_stmt, start_at, end_at)
    total_paid_orders = await session.scalar(paid_orders_stmt)

    non_gift_quantity = case((OrderItem.is_gift.is_not(True), OrderItem.quantity), else_=0)
    non_gift_revenue = case(
        (OrderItem.is_gift.is_not(True), OrderItem.quantity * OrderItem.price_per_unit),
        else_=0,
    )
    gift_quantity = case((OrderItem.is_gift.is_(True), OrderItem.quantity), else_=0)

    rows_stmt = (
        select(
            Product.id.label("product_id"),
            Product.name.label("product_name"),
            func.coalesce(func.sum(non_gift_quantity), 0).label("quantity_sold"),
            func.coalesce(func.sum(non_gift_revenue), 0).label("revenue"),
            func.coalesce(func.sum(gift_quantity), 0).label("gift_quantity"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.payment_status == PaymentStatus.paid)
        .group_by(Product.id, Product.name)
        .order_by(func.coalesce(func.sum(non_gift_revenue), 0).desc(), Product.name.asc())
    )
    rows_stmt = _apply_period_filter(rows_stmt, start_at, end_at)
    rows = (await session.execute(rows_stmt)).all()

    stats = aggregate_stats_rows(rows, int(total_paid_orders or 0), period)
    stats["limited"] = len(stats["products"]) > MAX_ANALYTICS_ROWS
    stats["products"] = stats["products"][:MAX_ANALYTICS_ROWS]
    return stats


def _payment_totals_subquery():
    return (
        select(
            OrderPayment.order_id.label("order_id"),
            func.coalesce(func.sum(OrderPayment.amount), 0).label("paid_amount"),
        )
        .group_by(OrderPayment.order_id)
        .subquery()
    )


def _debt_amount_expr(payment_totals):
    return func.coalesce(Order.total_amount, 0) - func.coalesce(payment_totals.c.paid_amount, 0)


def _age_days_expr():
    return func.floor(func.extract("epoch", func.now() - Order.created_at) / 86400)


async def get_debt_stats(session: AsyncSession, mode: str = "delivered") -> dict:
    payment_totals = _payment_totals_subquery()
    debt_amount = _debt_amount_expr(payment_totals)

    base_filters = [Order.payment_status != PaymentStatus.paid]
    if mode in {"delivered", "shops", "old7"}:
        base_filters.append(Order.delivery_status == DeliveryStatus.delivered)
    if mode == "old7":
        base_filters.append(Order.created_at <= datetime.now(BANGKOK_TZ) - timedelta(days=7))

    if mode == "shops":
        rows_stmt = (
            select(
                Shop.id.label("shop_id"),
                Shop.name.label("shop_name"),
                func.coalesce(func.sum(debt_amount), 0).label("debt_amount"),
                func.count(Order.id).label("order_count"),
            )
            .join(Shop, Shop.id == Order.shop_id)
            .outerjoin(payment_totals, payment_totals.c.order_id == Order.id)
            .where(*base_filters)
            .group_by(Shop.id, Shop.name)
            .order_by(func.coalesce(func.sum(debt_amount), 0).desc(), Shop.name.asc())
        )
        rows = (await session.execute(rows_stmt)).all()
        stats = aggregate_debt_shop_rows(rows, mode)
        stats["limited"] = len(stats["shops"]) > MAX_ANALYTICS_ROWS
        stats["shops"] = stats["shops"][:MAX_ANALYTICS_ROWS]
        return stats

    rows_stmt = (
        select(
            Order.id.label("order_id"),
            Order.display_number.label("display_number"),
            Shop.name.label("shop_name"),
            debt_amount.label("debt_amount"),
            Order.delivery_status.label("delivery_status"),
            Order.created_at.label("created_at"),
            _age_days_expr().label("age_days"),
        )
        .join(Shop, Shop.id == Order.shop_id)
        .outerjoin(payment_totals, payment_totals.c.order_id == Order.id)
        .where(*base_filters)
        .order_by(debt_amount.desc(), Order.created_at.asc())
    )
    rows = (await session.execute(rows_stmt)).all()
    stats = aggregate_debt_rows(rows, mode)
    stats["limited"] = len(stats["orders"]) > MAX_ANALYTICS_ROWS
    stats["orders"] = stats["orders"][:MAX_ANALYTICS_ROWS]
    return stats


async def get_shop_sales_stats(session: AsyncSession, period: str) -> dict:
    start_at, end_at = period_bounds(period)
    payment_totals = _payment_totals_subquery()

    non_gift_quantity = case((OrderItem.is_gift.is_not(True), OrderItem.quantity), else_=0)
    non_gift_revenue = case(
        (OrderItem.is_gift.is_not(True), OrderItem.quantity * OrderItem.price_per_unit),
        else_=0,
    )

    paid_filters = [Order.payment_status == PaymentStatus.paid]
    if start_at is not None:
        paid_filters.append(Order.created_at >= start_at)
    if end_at is not None:
        paid_filters.append(Order.created_at <= end_at)

    paid_sales = (
        select(
            Shop.id.label("shop_id"),
            Shop.name.label("shop_name"),
            func.coalesce(func.sum(non_gift_revenue), 0).label("revenue"),
            func.count(distinct(Order.id)).label("paid_orders"),
            func.coalesce(func.sum(non_gift_quantity), 0).label("quantity_sold"),
            func.max(Order.created_at).label("last_order_at"),
        )
        .select_from(Shop)
        .join(Order, Order.shop_id == Shop.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(*paid_filters)
        .group_by(Shop.id, Shop.name)
        .subquery()
    )

    unpaid_filters = [Order.payment_status != PaymentStatus.paid]
    if start_at is not None:
        unpaid_filters.append(Order.created_at >= start_at)
    if end_at is not None:
        unpaid_filters.append(Order.created_at <= end_at)

    unpaid_by_shop = (
        select(
            Order.shop_id.label("shop_id"),
            func.coalesce(func.sum(_debt_amount_expr(payment_totals)), 0).label("unpaid_amount"),
        )
        .outerjoin(payment_totals, payment_totals.c.order_id == Order.id)
        .where(*unpaid_filters)
        .group_by(Order.shop_id)
        .subquery()
    )

    rows_stmt = (
        select(
            paid_sales.c.shop_id,
            paid_sales.c.shop_name,
            paid_sales.c.revenue,
            paid_sales.c.paid_orders,
            paid_sales.c.quantity_sold,
            paid_sales.c.last_order_at,
            func.coalesce(unpaid_by_shop.c.unpaid_amount, 0).label("unpaid_amount"),
        )
        .outerjoin(unpaid_by_shop, unpaid_by_shop.c.shop_id == paid_sales.c.shop_id)
        .order_by(paid_sales.c.revenue.desc(), paid_sales.c.shop_name.asc())
    )
    rows = (await session.execute(rows_stmt)).all()
    stats = aggregate_shop_sales_rows(rows, period)
    stats["limited"] = len(stats["shops"]) > MAX_ANALYTICS_ROWS
    stats["shops"] = stats["shops"][:MAX_ANALYTICS_ROWS]
    return stats
