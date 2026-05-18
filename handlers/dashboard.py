import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryStatus
from handlers.orders import order_card_keyboard, order_card_text
from services.orders import bangkok_datetime, dashboard_orders, dashboard_day_bounds, format_dashboard_datetime, get_order

router = Router()
ORDER_CHAT_TYPES = (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP)


async def respond_to_message(message: Message, text: str, **kwargs):
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await message.reply(text, **kwargs)
    return await message.answer(text, **kwargs)


def is_today_order(order) -> bool:
    start, end = dashboard_day_bounds()
    created_at = order.created_at
    if created_at is None:
        return False
    created_at = bangkok_datetime(created_at)
    return start <= created_at < end


def dashboard_summary_text(orders) -> str:
    today_count = sum(1 for order in orders if is_today_order(order))
    pending_deliveries = sum(1 for order in orders if order.delivery_status != DeliveryStatus.delivered)
    processing_payments = sum(1 for order in orders if order.payment_status.value != "paid")
    return "\n".join(
        [
            "<b>Dashboard</b>",
            f"Today orders: <b>{today_count}</b>",
            f"Pending deliveries: <b>{pending_deliveries}</b>",
            f"Processing payments: <b>{processing_payments}</b>",
            "",
            "Latest 10 orders from today:",
        ]
    )


def order_state_emoji(order) -> str:
    if order.delivery_status == DeliveryStatus.delivered and order.payment_status.value == "paid":
        return "✅"
    return "⌛"


def dashboard_button_text(order) -> str:
    created_at = format_dashboard_datetime(order.created_at).replace(" ", " (") + ")"
    shop_name = order.shop.name[:18]
    return f"#{order.id} | 📅 {created_at} | {shop_name} | {order_state_emoji(order)}"[:64]


def dashboard_keyboard(orders) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=dashboard_button_text(order), callback_data=f"dash_order:{order.id}")]
            for order in orders
        ]
    )


@router.message(Command("dashboard"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def dashboard_command(message: Message, session: AsyncSession) -> None:
    try:
        orders = await dashboard_orders(session)
        if not orders:
            await respond_to_message(message, "No dashboard orders for today.")
            return
        await respond_to_message(
            message,
            dashboard_summary_text(orders),
            reply_markup=dashboard_keyboard(orders),
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Dashboard command failed")
        await respond_to_message(message, "Ошибка загрузки дашборда. Попробуйте позже.")


@router.callback_query(F.data.startswith("dash_order:"))
async def dashboard_open_order(callback: CallbackQuery, session: AsyncSession) -> None:
    try:
        raw_order_id = callback.data.split(":", 1)[1]
        try:
            order_id = int(raw_order_id)
        except ValueError:
            await callback.answer("Invalid order ID.", show_alert=True)
            return

        order = await get_order(session, order_id)
        delivered = order.delivery_status == DeliveryStatus.delivered
        await callback.message.edit_text(
            text=order_card_text(order),
            reply_markup=order_card_keyboard(order.id, delivered=delivered),
            parse_mode="HTML",
        )
        await callback.answer()
    except Exception:
        logging.exception("Dashboard order open handler failed")
        await callback.answer("Ошибка обработки запроса.", show_alert=True)
