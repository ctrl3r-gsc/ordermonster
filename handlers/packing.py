from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from services.orders import format_dashboard_datetime
from services.packing import packing_orders
from services.packing_core import packing_list_text, split_packing_messages


router = Router()
ORDER_CHAT_TYPES = (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP)


async def respond_to_message(message: Message, text: str, **kwargs):
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await message.reply(text, **kwargs)
    return await message.answer(text, **kwargs)


async def packing_messages(session: AsyncSession) -> list[str]:
    orders = await packing_orders(session)
    return split_packing_messages(packing_list_text(orders, format_dashboard_datetime))


@router.message(Command("packing", "pack", "assembly"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def packing_command(message: Message, session: AsyncSession) -> None:
    messages = await packing_messages(session)
    first, *rest = messages
    await respond_to_message(message, first, parse_mode="HTML")
    for chunk in rest:
        await message.answer(chunk, parse_mode="HTML")


@router.callback_query(F.data == "packing:list")
async def packing_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    messages = await packing_messages(session)
    first, *rest = messages
    await callback.message.edit_text(first, parse_mode="HTML")
    for chunk in rest:
        await callback.message.answer(chunk, parse_mode="HTML")
    await callback.answer()
