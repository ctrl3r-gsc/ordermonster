from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import DeliveryStatus, Order, OrderItem


def order_needs_packing_filter():
    return (
        (Order.delivery_status == DeliveryStatus.pending_shipment)
        & or_(Order.tracking_number.is_(None), Order.tracking_number == "")
    )


async def packing_orders(session: AsyncSession) -> list[Order]:
    return list(
        (
            await session.scalars(
                select(Order)
                .where(order_needs_packing_filter())
                .options(
                    selectinload(Order.shop),
                    selectinload(Order.items).selectinload(OrderItem.product),
                )
                .order_by(Order.display_number.asc().nulls_last(), Order.created_at.asc(), Order.id.asc())
            )
        ).all()
    )
