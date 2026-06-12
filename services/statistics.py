from datetime import datetime

from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Order, OrderItem, PaymentStatus, Product
from services.statistics_core import aggregate_stats_rows, period_bounds


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

    return aggregate_stats_rows(rows, int(total_paid_orders or 0), period)
