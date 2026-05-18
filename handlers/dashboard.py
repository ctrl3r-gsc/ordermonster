from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryStatus, Order
from services.orders import dashboard_orders, dashboard_day_bounds

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
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=start.tzinfo)
    else:
        created_at = created_at.astimezone(start.tzinfo)
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


def delivery_emoji(status) -> str:
    if status == DeliveryStatus.delivered:
        return "🚚"
    return "⏳"


def payment_emoji(payment_status) -> str:
    if payment_status == "paid":
        return "💰"
    if payment_status == "partially_paid":
        return "⚠️"
    return "❌"


def dashboard_button_text(order) -> str:
    id_part = f"# {order.id}".ljust(5)
    shop_part = f"🏪 {order.shop.name[:18]}".ljust(22)
    delivery_part = delivery_emoji(order.delivery_status)
    payment_part = payment_emoji(order.payment_status.value)
    return f"{id_part} | {shop_part} | {delivery_part} | {payment_part}"


def delete_confirmation_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_del:{order_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_del:{order_id}")],
        ]
    )


def dashboard_keyboard(orders) -> InlineKeyboardMarkup:
    rows = []
    for order in orders:
        rows.append(
            [InlineKeyboardButton(text=dashboard_button_text(order), callback_data=f"dash_delete:{order.id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("dashboard"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def dashboard_command(message: Message, session: AsyncSession) -> None:
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


@router.callback_query(F.data.startswith("dash_delete:"))
async def dashboard_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    raw_order_id = callback.data.split(":", 1)[1]
    try:
        order_id = int(raw_order_id)
    except ValueError:
        await callback.answer("Invalid order ID.", show_alert=True)
        return

    order = await session.get(Order, order_id)
    if order is None:
        await callback.answer("Order not found.", show_alert=True)
        return

    await callback.message.edit_text(
        f"Удалить заказ #{order_id}?",
        reply_markup=delete_confirmation_keyboard(order_id),
    )
    await callback.answer()
