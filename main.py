import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.middlewares import AllowedUsersMiddleware, DbSessionMiddleware
from config import get_settings
from db import SessionLocal, init_db
from handlers import router
from services.catalog import seed_current_catalog


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    await init_db()
    async with SessionLocal() as session:
        await seed_current_catalog(session)
        await session.commit()

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(AllowedUsersMiddleware(settings.allowed_users))
    dp.callback_query.middleware(AllowedUsersMiddleware(settings.allowed_users))
    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(router)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
