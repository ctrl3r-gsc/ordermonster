import asyncio
import json
from html import escape
from urllib.request import Request, urlopen

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from config import get_settings

router = Router()
ASSEMBLY_URL = "https://assembly.gosuay.com"
STOCK_PAGE_SIZE = 20


class StockSearchFlow(StatesGroup):
    query = State()


def stock_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ingredients", callback_data="stock:list:ingredient:0"),
         InlineKeyboardButton(text="Packaging", callback_data="stock:list:packaging:0")],
        [InlineKeyboardButton(text="Search", callback_data="stock:search"),
         InlineKeyboardButton(text="Show balances", callback_data="stock:balances:0")],
    ])


def stock_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Back to Stock", callback_data="stock:menu")]])


def stock_list_keyboard(category: str | None, show_balances: bool, page: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    pages = max(1, (total + STOCK_PAGE_SIZE - 1) // STOCK_PAGE_SIZE)
    navigation = []
    if page > 0:
        suffix = "balances" if show_balances else f"list:{category}"
        navigation.append(InlineKeyboardButton(text="← Prev", callback_data=f"stock:{suffix}:{page - 1}"))
    if page + 1 < pages:
        suffix = "balances" if show_balances else f"list:{category}"
        navigation.append(InlineKeyboardButton(text="Next →", callback_data=f"stock:{suffix}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="← Back to Stock", callback_data="stock:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fetch_stock_directory() -> list[dict]:
    settings = get_settings()
    if not settings.assembly_api_url or not settings.assembly_api_token:
        raise RuntimeError("Assembly integration is not configured")
    request = Request(
        settings.assembly_api_url.rstrip("/") + "/internal/stock-directory",
        headers={"Authorization": "Bearer " + settings.assembly_api_token},
    )
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read(512 * 1024))
    return payload.get("items", [])


async def fetch_stock_directory() -> list[dict]:
    return await asyncio.to_thread(_fetch_stock_directory)


def stock_menu_text() -> str:
    return "<b>Stock directory</b>\n\nChoose a section:"


def stock_items_text(items: list[dict], title: str, page: int, show_balances: bool) -> str:
    pages = max(1, (len(items) + STOCK_PAGE_SIZE - 1) // STOCK_PAGE_SIZE)
    visible = items[page * STOCK_PAGE_SIZE:(page + 1) * STOCK_PAGE_SIZE]
    lines = [f"<b>{escape(title)}</b>", f"Page {page + 1} of {pages}", ""]
    if not visible:
        lines.append("No matching stock items.")
    else:
        for item in visible:
            suffix = f" — {escape(item['balance'])} {escape(item['unit'])}" if show_balances else f" — {escape(item['unit'])}"
            lines.append(f"• {escape(item['name'])}{suffix}")
    return "\n".join(lines)


async def _edit_stock_menu(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.edit_text(stock_menu_text(), reply_markup=stock_menu_keyboard(), parse_mode="HTML")


@router.message(Command("stock"))
async def stock_command(message: Message, command: CommandObject, state: FSMContext) -> None:
    query = (command.args or "").strip()
    if query:
        await _render_stock_search(message, query)
        return
    await state.clear()
    await message.answer(stock_menu_text(), reply_markup=stock_menu_keyboard(), parse_mode="HTML")


async def _render_stock_search(target: Message, query: str) -> None:
    try:
        items = await fetch_stock_directory()
    except Exception:
        await target.answer("Could not load the Stock directory. Try again later.")
        return
    normalized = query.casefold()
    matches = [item for item in items if normalized in item["name"].casefold()]
    text = stock_items_text(matches, f"Search: {query}", 0, False)
    await target.answer(text, reply_markup=stock_back_keyboard(), parse_mode="HTML")


@router.callback_query(F.data == "stock:menu")
async def stock_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _edit_stock_menu(callback)
    await callback.answer()


@router.callback_query(F.data == "stock:search")
async def stock_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StockSearchFlow.query)
    if callback.message:
        await callback.message.edit_text(
            "<b>Stock search</b>\n\nSend a word from the system name, for example <code>mango</code> or <code>side</code>.",
            reply_markup=stock_back_keyboard(), parse_mode="HTML",
        )
    await callback.answer()


@router.message(StockSearchFlow.query)
async def stock_search_query(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if not query:
        await message.answer("Send a search word or press Back to Stock.", reply_markup=stock_back_keyboard())
        return
    await state.clear()
    await _render_stock_search(message, query)


async def _render_stock_list(callback: CallbackQuery, category: str, page: int) -> None:
    try:
        items = [item for item in await fetch_stock_directory() if item["type"] == category]
    except Exception:
        if callback.message:
            await callback.message.edit_text("Could not load the Stock directory. Try again later.", reply_markup=stock_back_keyboard())
        return
    title = "Ingredients" if category == "ingredient" else "Packaging"
    page = max(0, min(page, max(0, (len(items) - 1) // STOCK_PAGE_SIZE)))
    if callback.message:
        await callback.message.edit_text(
            stock_items_text(items, title, page, False),
            reply_markup=stock_list_keyboard(category, False, page, len(items)), parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("stock:list:"))
async def stock_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, _, category, raw_page = callback.data.split(":")
    try:
        page = int(raw_page)
    except ValueError:
        page = 0
    await _render_stock_list(callback, category, page)
    await callback.answer()


@router.callback_query(F.data.startswith("stock:balances:"))
async def stock_balances(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        items = await fetch_stock_directory()
    except Exception:
        if callback.message:
            await callback.message.edit_text("Could not load the Stock directory. Try again later.", reply_markup=stock_back_keyboard())
        await callback.answer()
        return
    try:
        page = max(0, int(callback.data.rsplit(":", 1)[1]))
    except ValueError:
        page = 0
    page = min(page, max(0, (len(items) - 1) // STOCK_PAGE_SIZE))
    if callback.message:
        await callback.message.edit_text(
            stock_items_text(items, "Stock balances", page, True),
            reply_markup=stock_list_keyboard(None, True, page, len(items)), parse_mode="HTML",
        )
    await callback.answer()


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
