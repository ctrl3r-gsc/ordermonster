import logging
from datetime import datetime
from decimal import Decimal
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from services.statistics import get_debt_stats, get_product_sales_stats, get_shop_sales_stats
from services.statistics_core import BANGKOK_TZ


router = Router()
ORDER_CHAT_TYPES = (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP)
PERIODS = ("today", "week", "month", "all")
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
DEBT_MODE_LABELS = {
    "delivered": "Delivered unpaid",
    "all": "All unpaid",
    "shops": "By shop",
    "old7": "Older than 7 days",
}


async def respond_to_message(message: Message, text: str, **kwargs):
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await message.reply(text, **kwargs)
    return await message.answer(text, **kwargs)


def ui_lang(language_code: str | None) -> str:
    return "ru" if (language_code or "").lower().startswith("ru") else "en"


def money(value: Decimal | int | str | None) -> str:
    amount = Decimal(str(value or 0))
    return f"{amount:,.2f}".rstrip("0").rstrip(".")


def local_date(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=BANGKOK_TZ)
    return value.astimezone(BANGKOK_TZ).strftime("%Y-%m-%d")


def period_keyboard(prefix: str, selected_period: str = "month", lang: str = "en") -> InlineKeyboardMarkup:
    period_row = []
    for period in PERIODS:
        label = PERIOD_LABELS[lang][period]
        if period == selected_period:
            label = f"* {label}"
        period_row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{period}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            period_row,
            [InlineKeyboardButton(text="Close", callback_data=f"{prefix}:close")],
        ]
    )


def debts_keyboard(selected_mode: str = "delivered") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("* " if selected_mode == "delivered" else "") + DEBT_MODE_LABELS["delivered"],
                callback_data="stats:debts:delivered",
            ),
            InlineKeyboardButton(
                text=("* " if selected_mode == "all" else "") + DEBT_MODE_LABELS["all"],
                callback_data="stats:debts:all",
            ),
        ],
        [
            InlineKeyboardButton(
                text=("* " if selected_mode == "shops" else "") + DEBT_MODE_LABELS["shops"],
                callback_data="stats:debts:shops",
            ),
            InlineKeyboardButton(
                text=("* " if selected_mode == "old7" else "") + DEBT_MODE_LABELS["old7"],
                callback_data="stats:debts:old7",
            ),
        ],
        [InlineKeyboardButton(text="Close", callback_data="stats:debts:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_statistics_text(stats: dict, lang: str = "en") -> str:
    period_label = PERIOD_LABELS[lang].get(stats["period"], stats["period"])
    lines = [f"📊 <b>Product Statistics — {escape(period_label)}</b>", ""]
    if not stats["products"]:
        lines.append("No paid product sales for this period yet.")
        return "\n".join(lines)

    lines.extend(
        [
            f"💰 Total revenue: <b>{money(stats['total_revenue'])} THB</b>",
            f"🧾 Paid orders: <b>{stats['total_paid_orders']}</b>",
            f"📦 Products sold: <b>{stats['total_quantity']} pcs</b>",
            "",
            "<b>Top products:</b>",
        ]
    )
    for index, product in enumerate(stats["products"], start=1):
        lines.extend(
            [
                "",
                f"{index}. <b>{escape(product['product_name'])}</b>",
                f"   Qty: {product['quantity_sold']} pcs",
                f"   Revenue: {money(product['revenue'])} THB",
            ]
        )
        if product.get("gift_quantity"):
            lines.append(f"   Gifts: {product['gift_quantity']} pcs")
    if stats.get("limited"):
        lines.extend(["", "Showing top 20 results."])
    return "\n".join(lines)


def debt_statistics_text(stats: dict) -> str:
    mode = stats.get("mode", "delivered")
    title = DEBT_MODE_LABELS.get(mode, mode)
    lines = [f"💸 <b>Debts — {escape(title)}</b>", ""]

    if mode == "shops":
        if not stats.get("shops"):
            lines.append("No delivered unpaid debts found.")
            return "\n".join(lines)
        lines.extend(
            [
                f"Total debt: <b>{money(stats['total_debt'])} THB</b>",
                f"Orders: <b>{stats['order_count']}</b>",
                "",
                "<b>By shop:</b>",
            ]
        )
        for index, shop in enumerate(stats["shops"], start=1):
            lines.append(
                f"{index}. {escape(shop['shop_name'])} — {money(shop['debt_amount'])} THB — "
                f"{shop['order_count']} order(s)"
            )
        if stats.get("limited"):
            lines.extend(["", "Showing top 20 results."])
        return "\n".join(lines)

    if not stats.get("orders"):
        lines.append("No unpaid orders found for this filter.")
        return "\n".join(lines)

    lines.extend(
        [
            f"Total debt: <b>{money(stats['total_debt'])} THB</b>",
            f"Orders: <b>{stats['order_count']}</b>",
        ]
    )
    for index, order in enumerate(stats["orders"], start=1):
        delivery_status = getattr(order["delivery_status"], "value", order["delivery_status"])
        lines.extend(
            [
                "",
                f"{index}. <b>Order #{order['display_number']}</b>",
                f"   Shop: {escape(order['shop_name'])}",
                f"   Amount: {money(order['debt_amount'])} THB",
                f"   Delivery: {escape(str(delivery_status))}",
                f"   Created: {local_date(order['created_at'])} ({order['age_days']}d ago)",
            ]
        )
    if stats.get("limited"):
        lines.extend(["", "Showing top 20 results."])
    return "\n".join(lines)


def shop_statistics_text(stats: dict, lang: str = "en") -> str:
    period_label = PERIOD_LABELS[lang].get(stats["period"], stats["period"])
    lines = [f"🏪 <b>Shop Statistics — {escape(period_label)}</b>", ""]
    if not stats["shops"]:
        lines.append("No paid shop sales for this period yet.")
        return "\n".join(lines)

    lines.extend(
        [
            f"💰 Total revenue: <b>{money(stats['total_revenue'])} THB</b>",
            f"🧾 Paid orders: <b>{stats['total_paid_orders']}</b>",
            f"🏪 Active shops: <b>{stats['active_shops']}</b>",
            "",
            "<b>Top shops:</b>",
        ]
    )
    for index, shop in enumerate(stats["shops"], start=1):
        lines.extend(
            [
                "",
                f"{index}. <b>{escape(shop['shop_name'])}</b>",
                f"   Revenue: {money(shop['revenue'])} THB",
                f"   Orders: {shop['paid_orders']}",
                f"   Average order: {money(shop['average_order'])} THB",
                f"   Products sold: {shop['quantity_sold']} pcs",
                f"   Last order: {local_date(shop['last_order_at'])}",
                f"   Unpaid: {money(shop['unpaid_amount'])} THB",
            ]
        )
    if stats.get("limited"):
        lines.extend(["", "Showing top 20 results."])
    return "\n".join(lines)


async def show_product_statistics(message: Message, session: AsyncSession, period: str = "month") -> None:
    lang = ui_lang(message.from_user.language_code if message.from_user else None)
    stats = await get_product_sales_stats(session, period)
    await respond_to_message(
        message,
        product_statistics_text(stats, lang),
        reply_markup=period_keyboard("stats:products", period, lang),
        parse_mode="HTML",
    )


async def edit_product_statistics(callback: CallbackQuery, session: AsyncSession, period: str) -> None:
    lang = ui_lang(callback.from_user.language_code if callback.from_user else None)
    stats = await get_product_sales_stats(session, period)
    await callback.message.edit_text(
        product_statistics_text(stats, lang),
        reply_markup=period_keyboard("stats:products", period, lang),
        parse_mode="HTML",
    )


async def show_debt_statistics(message: Message, session: AsyncSession, mode: str = "delivered") -> None:
    stats = await get_debt_stats(session, mode)
    await respond_to_message(
        message,
        debt_statistics_text(stats),
        reply_markup=debts_keyboard(mode),
        parse_mode="HTML",
    )


async def edit_debt_statistics(callback: CallbackQuery, session: AsyncSession, mode: str) -> None:
    stats = await get_debt_stats(session, mode)
    await callback.message.edit_text(
        debt_statistics_text(stats),
        reply_markup=debts_keyboard(mode),
        parse_mode="HTML",
    )


async def show_shop_statistics(message: Message, session: AsyncSession, period: str = "month") -> None:
    lang = ui_lang(message.from_user.language_code if message.from_user else None)
    stats = await get_shop_sales_stats(session, period)
    await respond_to_message(
        message,
        shop_statistics_text(stats, lang),
        reply_markup=period_keyboard("stats:shops", period, lang),
        parse_mode="HTML",
    )


async def edit_shop_statistics(callback: CallbackQuery, session: AsyncSession, period: str) -> None:
    lang = ui_lang(callback.from_user.language_code if callback.from_user else None)
    stats = await get_shop_sales_stats(session, period)
    await callback.message.edit_text(
        shop_statistics_text(stats, lang),
        reply_markup=period_keyboard("stats:shops", period, lang),
        parse_mode="HTML",
    )


@router.message(Command("statistics"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def statistics_command(message: Message, session: AsyncSession) -> None:
    try:
        await show_product_statistics(message, session, "month")
    except Exception:
        logging.exception("Product statistics command failed")
        await respond_to_message(message, "Product statistics failed to load. Try again later.")


@router.message(Command("debts"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def debts_command(message: Message, session: AsyncSession) -> None:
    try:
        await show_debt_statistics(message, session, "delivered")
    except Exception:
        logging.exception("Debts command failed")
        await respond_to_message(message, "Debts failed to load. Try again later.")


@router.message(Command("statistics_shops"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def statistics_shops_command(message: Message, session: AsyncSession) -> None:
    try:
        await show_shop_statistics(message, session, "month")
    except Exception:
        logging.exception("Shop statistics command failed")
        await respond_to_message(message, "Shop statistics failed to load. Try again later.")


@router.callback_query(F.data.startswith("stats:products:"))
async def product_statistics_period(callback: CallbackQuery, session: AsyncSession) -> None:
    period = callback.data.rsplit(":", 1)[1]
    if period == "close":
        await callback.message.delete()
        await callback.answer()
        return
    try:
        await edit_product_statistics(callback, session, period)
        await callback.answer()
    except Exception:
        logging.exception("Product statistics callback failed")
        await callback.answer("Product statistics failed to load.", show_alert=True)


@router.callback_query(F.data.startswith("stats:debts:"))
async def debt_statistics_mode(callback: CallbackQuery, session: AsyncSession) -> None:
    mode = callback.data.rsplit(":", 1)[1]
    if mode == "close":
        await callback.message.delete()
        await callback.answer()
        return
    try:
        await edit_debt_statistics(callback, session, mode)
        await callback.answer()
    except Exception:
        logging.exception("Debts callback failed")
        await callback.answer("Debts failed to load.", show_alert=True)


@router.callback_query(F.data.startswith("stats:shops:"))
async def shop_statistics_period(callback: CallbackQuery, session: AsyncSession) -> None:
    period = callback.data.rsplit(":", 1)[1]
    if period == "close":
        await callback.message.delete()
        await callback.answer()
        return
    try:
        await edit_shop_statistics(callback, session, period)
        await callback.answer()
    except Exception:
        logging.exception("Shop statistics callback failed")
        await callback.answer("Shop statistics failed to load.", show_alert=True)
