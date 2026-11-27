from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

router = Router()
ASSEMBLY_URL = "https://assembly.gosuay.com"


@router.message(Command("open_assembly"))
async def open_assembly(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Чтобы открыть Assembly, отправьте /open_assembly в личном чате с ботом.")
        return
    await message.answer(
        "Откройте Assembly кнопкой ниже:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть Assembly", web_app=WebAppInfo(url=ASSEMBLY_URL))
        ]]),
    )
