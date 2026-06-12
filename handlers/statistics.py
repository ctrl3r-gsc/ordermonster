import logging
from datetime import datetime
from decimal import Decimal
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from services.statistics import (
    get_debt_stats,
    get_followup_stats,
    get_product_performance_stats,
    get_product_sales_stats,
    get_sales_forecast,
    get_shop_analytics,
    get_shop_sales_stats,
)
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
FOLLOWUP_LABELS = {
    "attention": "Needs attention",
    "days7": "No orders 7+ days",
    "days14": "No orders 14+ days",
    "days30": "No orders 30+ days",
    "debts": "Has unpaid debt",
}
PRODUCT_MODE_LABELS = {
    "best_revenue": "Best by revenue",
    "best_qty": "Best by quantity",
    "slow7": "Slow 7 days",
    "slow14": "Slow 14 days",
    "slow30": "Slow 30 days",
    "no_sales_month": "No sales this month",
}
FORECAST_LABELS = {
    "month": "This month",
    "next7": "Next 7 days",
    "last7": "Based on last 7 days",
    "last30": "Based on last 30 days",
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


def days_since(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=BANGKOK_TZ)
    delta = datetime.now(BANGKOK_TZ) - value.astimezone(BANGKOK_TZ)
    return max(int(delta.total_seconds() // 86400), 0)


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


def followups_keyboard(selected_mode: str = "attention") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=("* " if selected_mode == "attention" else "") + FOLLOWUP_LABELS["attention"], callback_data="crm:followups:attention")],
        [
            InlineKeyboardButton(text=("* " if selected_mode == "days7" else "") + FOLLOWUP_LABELS["days7"], callback_data="crm:followups:days7"),
            InlineKeyboardButton(text=("* " if selected_mode == "days14" else "") + FOLLOWUP_LABELS["days14"], callback_data="crm:followups:days14"),
        ],
        [
            InlineKeyboardButton(text=("* " if selected_mode == "days30" else "") + FOLLOWUP_LABELS["days30"], callback_data="crm:followups:days30"),
            InlineKeyboardButton(text=("* " if selected_mode == "debts" else "") + FOLLOWUP_LABELS["debts"], callback_data="crm:followups:debts"),
        ],
        [InlineKeyboardButton(text="Close", callback_data="crm:followups:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_performance_keyboard(selected_mode: str = "best_revenue") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=("* " if selected_mode == "best_revenue" else "") + PRODUCT_MODE_LABELS["best_revenue"], callback_data="crm:products:best_revenue"),
            InlineKeyboardButton(text=("* " if selected_mode == "best_qty" else "") + PRODUCT_MODE_LABELS["best_qty"], callback_data="crm:products:best_qty"),
        ],
        [
            InlineKeyboardButton(text=("* " if selected_mode == "slow7" else "") + PRODUCT_MODE_LABELS["slow7"], callback_data="crm:products:slow7"),
            InlineKeyboardButton(text=("* " if selected_mode == "slow14" else "") + PRODUCT_MODE_LABELS["slow14"], callback_data="crm:products:slow14"),
        ],
        [
            InlineKeyboardButton(text=("* " if selected_mode == "slow30" else "") + PRODUCT_MODE_LABELS["slow30"], callback_data="crm:products:slow30"),
            InlineKeyboardButton(text=("* " if selected_mode == "no_sales_month" else "") + PRODUCT_MODE_LABELS["no_sales_month"], callback_data="crm:products:no_sales_month"),
        ],
        [InlineKeyboardButton(text="Close", callback_data="crm:products:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def forecast_keyboard(selected_mode: str = "month") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=("* " if selected_mode == "month" else "") + FORECAST_LABELS["month"], callback_data="crm:forecast:month"),
            InlineKeyboardButton(text=("* " if selected_mode == "next7" else "") + FORECAST_LABELS["next7"], callback_data="crm:forecast:next7"),
        ],
        [
            InlineKeyboardButton(text=("* " if selected_mode == "last7" else "") + FORECAST_LABELS["last7"], callback_data="crm:forecast:last7"),
            InlineKeyboardButton(text=("* " if selected_mode == "last30" else "") + FORECAST_LABELS["last30"], callback_data="crm:forecast:last30"),
        ],
        [InlineKeyboardButton(text="Close", callback_data="crm:forecast:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_analytics_keyboard(shop_id: int, selected_period: str = "month") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=("* " if selected_period == "month" else "") + "This month", callback_data=f"crm:shop_analytics:{shop_id}:month"),
                InlineKeyboardButton(text=("* " if selected_period == "all" else "") + "All time", callback_data=f"crm:shop_analytics:{shop_id}:all"),
            ],
            [InlineKeyboardButton(text="Back to shop", callback_data=f"crm:shop_analytics:{shop_id}:back")],
            [InlineKeyboardButton(text="Close", callback_data=f"crm:shop_analytics:{shop_id}:close")],
        ]
    )


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


def followups_text(stats: dict) -> str:
    mode = stats.get("mode", "attention")
    title = FOLLOWUP_LABELS.get(mode, mode)
    lines = [f"🔁 <b>Follow-ups — {escape(title)}</b>", ""]
    if not stats.get("shops"):
        lines.append("No follow-ups for this filter.")
        return "\n".join(lines)

    for index, shop in enumerate(stats["shops"], start=1):
        if shop["days_since_last_order"] is None:
            last_order = "never"
        else:
            last_order = f"{shop['days_since_last_order']} days ago"
        lines.extend(
            [
                f"{index}. <b>{escape(shop['shop_name'])}</b>",
                f"   Last order: {last_order}",
                f"   Last amount: {money(shop['last_order_amount'])} THB",
            ]
        )
        if shop.get("last_products"):
            lines.append(f"   Last products: {escape(shop['last_products'])}")
        lines.extend(
            [
                f"   Unpaid: {money(shop['unpaid_amount'])} THB",
                f"   Action: {escape(shop['suggested_action'])}",
                "",
            ]
        )
    if stats.get("limited"):
        lines.append("Showing top 20 results.")
    return "\n".join(lines).rstrip()


def product_performance_text(stats: dict) -> str:
    mode = stats.get("mode", "best_revenue")
    title = PRODUCT_MODE_LABELS.get(mode, mode)
    icon = "🏆" if mode.startswith("best") else "🐢"
    lines = [f"{icon} <b>Product Performance — {escape(title)}</b>", ""]
    if not stats.get("products"):
        lines.append("No products for this filter.")
        return "\n".join(lines)

    for index, product in enumerate(stats["products"], start=1):
        lines.extend(["", f"{index}. <b>{escape(product['product_name'])}</b>"])
        if mode.startswith("best"):
            lines.extend(
                [
                    f"   Revenue: {money(product['revenue'])} THB",
                    f"   Qty: {product['quantity_sold']} pcs",
                    f"   Paid orders: {product['paid_orders']}",
                ]
            )
        else:
            last_sold = local_date(product["last_sold_at"]) if product["last_sold_at"] else "never"
            lines.extend(
                [
                    f"   Last sold: {last_sold}",
                    f"   Revenue: {money(product['revenue'])} THB",
                    f"   Qty: {product['quantity_sold']} pcs",
                ]
            )
    if stats.get("limited"):
        lines.extend(["", "Showing top 20 results."])
    return "\n".join(lines)


def forecast_text(stats: dict) -> str:
    lines = [f"📈 <b>Forecast — {escape(stats['label'])}</b>", ""]
    lines.extend(
        [
            f"Revenue so far: <b>{money(stats['revenue_so_far'])} THB</b>",
            f"Days passed: <b>{stats['days_passed']} / {stats['total_days']}</b>",
            f"Daily average: <b>{money(stats['daily_average'])} THB</b>",
            "",
            f"Projected revenue: <b>{money(stats['projected_revenue'])} THB</b>",
            f"Projected paid orders: <b>{stats['projected_paid_orders']}</b>",
            f"Projected products sold: <b>{stats['projected_products_sold']} pcs</b>",
        ]
    )
    if stats.get("products"):
        lines.extend(["", "<b>Top projected products:</b>"])
        for index, product in enumerate(stats["products"], start=1):
            lines.append(f"{index}. {escape(product['product_name'])} — {product['projected_quantity']} pcs")
    if stats.get("limited_data"):
        lines.extend(["", "Forecast is based on limited data and should be used as an estimate."])
    if stats.get("limited"):
        lines.extend(["", "Showing top 20 results."])
    return "\n".join(lines)


def shop_analytics_text(stats: dict) -> str:
    last_order_days = days_since(stats["last_order_at"])
    if stats["last_order_at"] is None:
        last_order = "never"
    else:
        suffix = f", {last_order_days} days ago" if last_order_days is not None else ""
        last_order = f"{local_date(stats['last_order_at'])}{suffix}"
    period_label = "This month" if stats["period"] == "month" else "All time"
    lines = [
        f"📊 <b>Shop Analytics — {escape(stats['shop_name'])}</b>",
        "",
        f"Period: <b>{period_label}</b>",
        "",
        f"💰 Paid revenue: <b>{money(stats['paid_revenue'])} THB</b>",
        f"🧾 Paid orders: <b>{stats['paid_orders']}</b>",
        f"📦 Products sold: <b>{stats['quantity_sold']} pcs</b>",
        f"📊 Average order: <b>{money(stats['average_order'])} THB</b>",
        f"💸 Unpaid: <b>{money(stats['unpaid_amount'])} THB</b>",
        f"🕒 Last order: <b>{last_order}</b>",
    ]
    if stats.get("top_products"):
        lines.extend(["", "<b>Top products:</b>"])
        for index, product in enumerate(stats["top_products"], start=1):
            lines.append(
                f"{index}. {escape(product['product_name'])} — "
                f"{product['quantity_sold']} pcs — {money(product['revenue'])} THB"
            )
    if stats.get("last_products"):
        lines.extend(["", "<b>Last order:</b>"])
        for product in stats["last_products"]:
            lines.append(f"{escape(product['product_name'])} x{product['quantity']}")
    lines.extend(["", "<b>Suggested action:</b>", escape(stats["suggested_action"])])
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


async def show_followups(message: Message, session: AsyncSession, mode: str = "attention") -> None:
    stats = await get_followup_stats(session, mode)
    await respond_to_message(
        message,
        followups_text(stats),
        reply_markup=followups_keyboard(mode),
        parse_mode="HTML",
    )


async def edit_followups(callback: CallbackQuery, session: AsyncSession, mode: str) -> None:
    stats = await get_followup_stats(session, mode)
    await callback.message.edit_text(
        followups_text(stats),
        reply_markup=followups_keyboard(mode),
        parse_mode="HTML",
    )


async def show_product_performance(message: Message, session: AsyncSession, mode: str = "best_revenue") -> None:
    stats = await get_product_performance_stats(session, mode)
    await respond_to_message(
        message,
        product_performance_text(stats),
        reply_markup=product_performance_keyboard(mode),
        parse_mode="HTML",
    )


async def edit_product_performance(callback: CallbackQuery, session: AsyncSession, mode: str) -> None:
    stats = await get_product_performance_stats(session, mode)
    await callback.message.edit_text(
        product_performance_text(stats),
        reply_markup=product_performance_keyboard(mode),
        parse_mode="HTML",
    )


async def show_forecast(message: Message, session: AsyncSession, mode: str = "month") -> None:
    stats = await get_sales_forecast(session, mode)
    await respond_to_message(
        message,
        forecast_text(stats),
        reply_markup=forecast_keyboard(mode),
        parse_mode="HTML",
    )


async def edit_forecast(callback: CallbackQuery, session: AsyncSession, mode: str) -> None:
    stats = await get_sales_forecast(session, mode)
    await callback.message.edit_text(
        forecast_text(stats),
        reply_markup=forecast_keyboard(mode),
        parse_mode="HTML",
    )


async def edit_shop_analytics(callback: CallbackQuery, session: AsyncSession, shop_id: int, period: str = "month") -> None:
    stats = await get_shop_analytics(session, shop_id, period)
    await callback.message.edit_text(
        shop_analytics_text(stats),
        reply_markup=shop_analytics_keyboard(shop_id, period),
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


@router.message(Command("followups"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def followups_command(message: Message, session: AsyncSession) -> None:
    try:
        await show_followups(message, session, "attention")
    except Exception:
        logging.exception("Followups command failed")
        await respond_to_message(message, "Follow-ups failed to load. Try again later.")


@router.message(Command("slow_products"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def slow_products_command(message: Message, session: AsyncSession) -> None:
    try:
        await show_product_performance(message, session, "best_revenue")
    except Exception:
        logging.exception("Product performance command failed")
        await respond_to_message(message, "Product performance failed to load. Try again later.")


@router.message(Command("forecast"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def forecast_command(message: Message, session: AsyncSession) -> None:
    try:
        await show_forecast(message, session, "month")
    except Exception:
        logging.exception("Forecast command failed")
        await respond_to_message(message, "Forecast failed to load. Try again later.")


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


@router.callback_query(F.data.startswith("crm:followups:"))
async def followups_mode(callback: CallbackQuery, session: AsyncSession) -> None:
    mode = callback.data.rsplit(":", 1)[1]
    if mode == "close":
        await callback.message.delete()
        await callback.answer()
        return
    try:
        await edit_followups(callback, session, mode)
        await callback.answer()
    except Exception:
        logging.exception("Followups callback failed")
        await callback.answer("Follow-ups failed to load.", show_alert=True)


@router.callback_query(F.data.startswith("crm:products:"))
async def product_performance_mode(callback: CallbackQuery, session: AsyncSession) -> None:
    mode = callback.data.rsplit(":", 1)[1]
    if mode == "close":
        await callback.message.delete()
        await callback.answer()
        return
    try:
        await edit_product_performance(callback, session, mode)
        await callback.answer()
    except Exception:
        logging.exception("Product performance callback failed")
        await callback.answer("Product performance failed to load.", show_alert=True)


@router.callback_query(F.data.startswith("crm:forecast:"))
async def forecast_mode(callback: CallbackQuery, session: AsyncSession) -> None:
    mode = callback.data.rsplit(":", 1)[1]
    if mode == "close":
        await callback.message.delete()
        await callback.answer()
        return
    try:
        await edit_forecast(callback, session, mode)
        await callback.answer()
    except Exception:
        logging.exception("Forecast callback failed")
        await callback.answer("Forecast failed to load.", show_alert=True)


@router.callback_query(F.data.startswith("crm:shop_analytics:"))
async def shop_analytics_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Invalid shop analytics request.", show_alert=True)
        return
    _, _, raw_shop_id, action = parts
    try:
        shop_id = int(raw_shop_id)
    except ValueError:
        await callback.answer("Invalid shop ID.", show_alert=True)
        return
    if action == "close":
        await callback.message.delete()
        await callback.answer()
        return
    if action == "back":
        from handlers.dashboard import shop_details_keyboard, shop_details_text
        from db.models import Shop

        shop = await session.get(Shop, shop_id)
        if shop is None:
            await callback.answer("Shop not found.", show_alert=True)
            return
        await callback.message.edit_text(
            await shop_details_text(session, shop),
            reply_markup=shop_details_keyboard(shop.id),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    try:
        await edit_shop_analytics(callback, session, shop_id, action)
        await callback.answer()
    except Exception:
        logging.exception("Shop analytics callback failed")
        await callback.answer("Shop analytics failed to load.", show_alert=True)
