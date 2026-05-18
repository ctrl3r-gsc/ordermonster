import asyncio

from sqlalchemy import delete, func, select, update

from db import SessionLocal, init_db
from db.models import Order, Shop
from services.orders import sanitize_shop_name


async def deep_clean_shops() -> None:
    await init_db()
    merged_count = 0
    renamed_count = 0
    skipped_count = 0

    async with SessionLocal() as session:
        shops = list((await session.scalars(select(Shop).order_by(Shop.id.asc()))).all())

        for dirty_shop in shops:
            if dirty_shop.id is None:
                skipped_count += 1
                continue

            clean_name = sanitize_shop_name(dirty_shop.name)
            if not clean_name or clean_name == dirty_shop.name:
                skipped_count += 1
                continue

            clean_shop = await session.scalar(
                select(Shop).where(
                    func.lower(Shop.name) == clean_name.lower(),
                    Shop.id != dirty_shop.id,
                )
            )

            if clean_shop:
                await session.execute(
                    update(Order)
                    .where(Order.shop_id == dirty_shop.id)
                    .values(shop_id=clean_shop.id)
                )
                await session.execute(delete(Shop).where(Shop.id == dirty_shop.id))
                merged_count += 1
            else:
                dirty_shop.name = clean_name
                renamed_count += 1
                await session.flush()

        await session.commit()

    print(
        f"Deep cleaned shops: merged {merged_count}, "
        f"renamed {renamed_count}, skipped {skipped_count}."
    )


def main() -> None:
    asyncio.run(deep_clean_shops())


if __name__ == "__main__":
    main()
