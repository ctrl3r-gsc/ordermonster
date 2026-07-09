from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import DeliveryStatus, Order, OrderItem


async def packing_orders(session: AsyncSession) -> list[Order]:
    return list(
        (
            await session.scalars(
                select(Order)
                .where(Order.delivery_status != DeliveryStatus.delivered)
                .options(
                    selectinload(Order.shop),
                    selectinload(Order.items).selectinload(OrderItem.product),
                )
                .order_by(Order.display_number.asc().nulls_last(), Order.created_at.asc(), Order.id.asc())
            )
        ).all()
    )
