import logging
from decimal import Decimal
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from services.statistics import get_product_sales_stats


router = Router()
ORDER_CHAT_TYPES = (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP)
PERIOD_LABELS = {
    "en": {
        "today": "Today",
        "week": "This week",
        "month": "This month",
        "all": "All time",
    },
    "ru": {
        "today": "Сегодня",
        "week": "Неделя",
        "month": "Месяц",
        "all": "Всё время",
    },
}
PERIOD_CALLBACKS = {
    "today": "stats:today",
    "week": "stats:week",
    "month": "stats:month",
    "all": "stats:all",
}
UI_TEXT = {
    "en": {
        "title": "Statistics",
        "empty": "No paid sales for this period yet.",
        "total_revenue": "Total revenue",
        "paid_orders": "Paid orders",
        "products_sold": "Products sold",
        "top_products": "Top products",
        "qty": "Qty",
        "revenue": "Revenue",
        "gifts": "Gifts",
        "back": "Back",
        "close": "Close",
        "back_text": "Use /dashboard to open the dashboard or /statistics to reopen statistics.",
    },
    "ru": {
        "title": "Статистика",
        "empty": "За этот период пока нет оплаченных продаж.",
        "total_revenue": "Выручка",
        "paid_orders": "Оплаченные заказы",
        "products_sold": "Продано товаров",
        "top_products": "Топ товаров",
        "qty": "Кол-во",
        "revenue": "Выручка",
        "gifts": "Подарки",
        "back": "Назад",
        "close": "Закрыть",
        "back_text": "Используйте /dashboard для панели заказов или /statistics, чтобы открыть статистику снова.",
    },
}


async def respond_to_message(message: Message, text: str, **kwargs):
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await message.reply(text, **kwargs)
    return await message.answer(text, **kwargs)


def money(value: Decimal | int | str | None) -> str:
    amount = Decimal(str(value or 0))
    return f"{amount:,.2f}".rstrip("0").rstrip(".")


def ui_lang(language_code: str | None) -> str:
    return "ru" if (language_code or "").lower().startswith("ru") else "en"


def statistics_keyboard(selected_period: str = "month", lang: str = "en") -> InlineKeyboardMarkup:
    period_row = []
    for period in ("today", "week", "month", "all"):
        label = PERIOD_LABELS[lang][period]
        if period == selected_period:
            label = f"• {label}"
        period_row.append(InlineKeyboardButton(text=label, callback_data=PERIOD_CALLBACKS[period]))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            period_row,
            [InlineKeyboardButton(text=UI_TEXT[lang]["back"], callback_data="stats:back")],
            [InlineKeyboardButton(text=UI_TEXT[lang]["close"], callback_data="stats:close")],
        ]
    )


def statistics_text(stats: dict, lang: str = "en") -> str:
    text = UI_TEXT[lang]
    period_label = PERIOD_LABELS[lang].get(stats["period"], stats["period"])
    lines = [f"📊 <b>{text['title']} — {escape(period_label)}</b>", ""]
    if not stats["products"]:
        lines.append(text["empty"])
        return "\n".join(lines)

    lines.extend(
        [
            f"💰 {text['total_revenue']}: <b>{money(stats['total_revenue'])} THB</b>",
            f"🧾 {text['paid_orders']}: <b>{stats['total_paid_orders']}</b>",
            f"📦 {text['products_sold']}: <b>{stats['total_quantity']} pcs</b>",
            "",
            f"<b>{text['top_products']}:</b>",
        ]
    )
    for index, product in enumerate(stats["products"], start=1):
        lines.extend(
            [
                "",
                f"{index}. <b>{escape(product['product_name'])}</b>",
                f"   {text['qty']}: {product['quantity_sold']} pcs",
                f"   {text['revenue']}: {money(product['revenue'])} THB",
            ]
        )
        if product.get("gift_quantity"):
            lines.append(f"   {text['gifts']}: {product['gift_quantity']} pcs")
    return "\n".join(lines)


async def render_statistics_message(message: Message, session: AsyncSession, period: str = "month") -> None:
    lang = ui_lang(message.from_user.language_code if message.from_user else None)
    stats = await get_product_sales_stats(session, period)
    await respond_to_message(
        message,
        statistics_text(stats, lang),
        reply_markup=statistics_keyboard(period, lang),
        parse_mode="HTML",
    )


async def render_statistics_callback(callback: CallbackQuery, session: AsyncSession, period: str = "month") -> None:
    lang = ui_lang(callback.from_user.language_code if callback.from_user else None)
    stats = await get_product_sales_stats(session, period)
    await callback.message.edit_text(
        statistics_text(stats, lang),
        reply_markup=statistics_keyboard(period, lang),
        parse_mode="HTML",
    )


@router.message(Command("statistics"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def statistics_command(message: Message, session: AsyncSession) -> None:
    try:
        await render_statistics_message(message, session, "month")
    except Exception:
        logging.exception("Statistics command failed")
        await respond_to_message(message, "Statistics failed to load. Try again later.")


@router.callback_query(F.data.in_(set(PERIOD_CALLBACKS.values())))
async def statistics_period(callback: CallbackQuery, session: AsyncSession) -> None:
    period = callback.data.split(":", 1)[1]
    try:
        await render_statistics_callback(callback, session, period)
        await callback.answer()
    except Exception:
        logging.exception("Statistics period callback failed")
        await callback.answer("Statistics failed to load.", show_alert=True)


@router.callback_query(F.data == "stats:back")
async def statistics_back(callback: CallbackQuery) -> None:
    lang = ui_lang(callback.from_user.language_code if callback.from_user else None)
    await callback.message.edit_text(UI_TEXT[lang]["back_text"])
    await callback.answer()


@router.callback_query(F.data == "stats:close")
async def statistics_close(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer()
