import logging

from aiogram import Bot, Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from handlers.dashboard import send_dashboard_to_chat
from services.notifications import (
    NOTIFICATION_DASHBOARD_CALLBACK,
    disable_order_notifications,
    get_notification_chat_id,
    send_notification_test,
    set_notification_chat_id,
)


router = Router()
logger = logging.getLogger(__name__)


async def _is_notification_manager(message: Message) -> bool:
    allowed_users = get_settings().allowed_users
    if not allowed_users:
        return True
    return bool(message.from_user and message.from_user.id in allowed_users)


@router.message(Command("set_notifications_here"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def set_notifications_here(message: Message, session: AsyncSession) -> None:
    if not await _is_notification_manager(message):
        await message.reply("Only allowed users can change notification settings.")
        return
    await set_notification_chat_id(session, message.chat.id)
    await session.commit()
    await message.reply("✅ Order notifications enabled for this chat.")


@router.message(Command("set_notifications_here"), F.chat.type == ChatType.PRIVATE)
async def set_notifications_private(message: Message) -> None:
    await message.answer("Please run this command inside the group chat where notifications should be sent.")


@router.message(Command("notification_status"))
async def notification_status(message: Message, session: AsyncSession) -> None:
    chat_id = await get_notification_chat_id(session)
    if chat_id is None:
        await message.reply("Order notifications are disabled. Run /set_notifications_here in the group chat.")
        return
    await message.reply(f"Order notifications are enabled.\nConfigured chat_id: <code>{chat_id}</code>", parse_mode="HTML")


@router.message(Command("notification_test"))
async def notification_test(message: Message, bot: Bot, session: AsyncSession) -> None:
    try:
        sent = await send_notification_test(bot, session)
    except Exception:
        logger.exception("Failed to send test notification")
        await message.reply("Could not send the test notification. Check bot permissions in the configured group.")
        return
    if not sent:
        await message.reply("No notification chat is configured. Run /set_notifications_here in the group.")
        return
    await message.reply("Test notification sent.")


@router.callback_query(F.data == NOTIFICATION_DASHBOARD_CALLBACK)
async def notification_dashboard(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    await callback.answer()
    if callback.message is None:
        return
    try:
        await send_dashboard_to_chat(bot, session, callback.message.chat.id)
    except Exception:
        logger.exception("Failed to send dashboard from notification callback")
        await callback.message.answer("Dashboard failed to load. Try again later.")


@router.message(Command("disable_notifications"))
async def disable_notifications(message: Message, session: AsyncSession) -> None:
    if not await _is_notification_manager(message):
        await message.reply("Only allowed users can change notification settings.")
        return
    await disable_order_notifications(session)
    await session.commit()
    await message.reply("Notifications disabled.")
