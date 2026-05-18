import asyncio
from datetime import date

from sqlalchemy import select

from db import SessionLocal, init_db
from db.models import DeliveryStatus, Order, PaymentStatus


async def main() -> None:
    await init_db()
    today = date.today()
    async with SessionLocal() as session:
        result = await session.scalars(select(Order))
        orders = result.all()

        for order in orders:
            if order.created_at is None:
                continue
            created_date = order.created_at.date()
            if created_date < today:
                order.delivery_status = DeliveryStatus.delivered
                order.payment_status = PaymentStatus.paid
            else:
                await session.delete(order)

        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
