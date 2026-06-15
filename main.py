import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.strategy import FSMStrategy

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

    async def on_startup(*_):
        await bot.set_my_commands(
            [
                BotCommand(command="dashboard", description="Quick order overview"),
                BotCommand(command="statistics", description="Product sales statistics"),
                BotCommand(command="debts", description="Delivered and unpaid orders"),
                BotCommand(command="statistics_shops", description="Shop sales statistics"),
                BotCommand(command="followups", description="Shops that need follow-up"),
                BotCommand(command="slow_products", description="Product performance and slow movers"),
                BotCommand(command="forecast", description="Sales forecast"),
                BotCommand(command="set_notifications_here", description="Enable order notifications for this chat"),
                BotCommand(command="notification_status", description="Show order notification settings"),
                BotCommand(command="notification_test", description="Send a test order notification"),
                BotCommand(command="disable_notifications", description="Disable order notifications"),
            ]
        )

    dp = Dispatcher(storage=MemoryStorage(), fsm_strategy=FSMStrategy.USER_IN_CHAT)
    dp.startup.register(on_startup)
    dp.message.middleware(AllowedUsersMiddleware(settings.allowed_users, settings.allowed_chats))
    dp.callback_query.middleware(AllowedUsersMiddleware(settings.allowed_users, settings.allowed_chats))
    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(router)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
