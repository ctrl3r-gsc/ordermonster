import asyncio

from sqlalchemy import delete, func, select, update

from db import SessionLocal, init_db
from db.models import Order, Shop
from services.orders import sanitize_shop_name


async def clean_shops() -> None:
    await init_db()
    merged_count = 0
    renamed_count = 0
    skipped_count = 0

    async with SessionLocal() as session:
        messy_shops = list(
            (
                await session.scalars(
                    select(Shop)
                    .where(func.lower(Shop.name).contains("order for:"))
                    .order_by(Shop.id.asc())
                )
            ).all()
        )

        for messy_shop in messy_shops:
            clean_name = sanitize_shop_name(messy_shop.name)
            if not clean_name or clean_name.lower() == messy_shop.name.lower():
                skipped_count += 1
                continue

            clean_shop = await session.scalar(
                select(Shop).where(
                    func.lower(Shop.name) == clean_name.lower(),
                    Shop.id != messy_shop.id,
                )
            )

            if clean_shop:
                await session.execute(
                    update(Order)
                    .where(Order.shop_id == messy_shop.id)
                    .values(shop_id=clean_shop.id)
                )
                await session.execute(delete(Shop).where(Shop.id == messy_shop.id))
                merged_count += 1
            else:
                messy_shop.name = clean_name
                renamed_count += 1

        await session.commit()

    print(
        f"Cleaned shops: merged {merged_count}, "
        f"renamed {renamed_count}, skipped {skipped_count}."
    )


def main() -> None:
    asyncio.run(clean_shops())


if __name__ == "__main__":
    main()
