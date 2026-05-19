import logging
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryStatus, Order, OrderItem, OrderPayment, Shop
from handlers.orders import order_card_keyboard, order_card_text
from services.orders import all_shops, bangkok_datetime, dashboard_orders, dashboard_day_bounds, format_dashboard_datetime, get_order

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
    rows = [
        [InlineKeyboardButton(text=dashboard_button_text(order), callback_data=f"dash_order:{order.id}")]
        for order in orders
    ]
    rows.append([InlineKeyboardButton(text="🏪 Магазины", callback_data="shops:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dashboard_empty_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏪 Магазины", callback_data="shops:list")],
        ]
    )


def shops_keyboard(shops) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=shop.name[:40], callback_data=f"shops:open:{shop.id}")] for shop in shops]
    rows.append([InlineKeyboardButton(text="🔙 Dashboard", callback_data="shops:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_details_keyboard(shop_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Удалить магазин", callback_data=f"shops:delete:{shop_id}")],
            [InlineKeyboardButton(text="🔙 К списку магазинов", callback_data="shops:list")],
        ]
    )


def shop_delete_confirmation_keyboard(shop_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"shops:confirm_delete:{shop_id}")],
            [InlineKeyboardButton(text="🔙 Отмена", callback_data=f"shops:cancel_delete:{shop_id}")],
        ]
    )


async def shop_order_count(session: AsyncSession, shop_id: int) -> int:
    return await session.scalar(select(func.count(Order.id)).where(Order.shop_id == shop_id)) or 0


async def shop_details_text(session: AsyncSession, shop: Shop) -> str:
    order_count = await shop_order_count(session, shop.id)
    address = escape(shop.address) if shop.address else "не указан"
    return "\n".join(
        [
            f"🏪 <b>{escape(shop.name)}</b>",
            f"📍 Адрес: {address}",
            f"📦 Заказов: <b>{order_count}</b>",
        ]
    )


async def render_shops_list(callback: CallbackQuery, session: AsyncSession, prefix: str | None = None) -> None:
    shops = await all_shops(session)
    text = "🏪 <b>Магазины</b>"
    if prefix:
        text = f"{escape(prefix)}\n\n{text}"
    if not shops:
        await callback.message.edit_text(
            f"{text}\n\nСписок магазинов пуст.",
            reply_markup=dashboard_empty_keyboard(),
            parse_mode="HTML",
        )
        return
    await callback.message.edit_text(text, reply_markup=shops_keyboard(shops), parse_mode="HTML")


async def render_dashboard(callback: CallbackQuery, session: AsyncSession) -> None:
    orders = await dashboard_orders(session)
    if not orders:
        await callback.message.edit_text("No dashboard orders for today.", reply_markup=dashboard_empty_keyboard())
        return
    await callback.message.edit_text(
        dashboard_summary_text(orders),
        reply_markup=dashboard_keyboard(orders),
        parse_mode="HTML",
    )


async def delete_shop_with_orders(session: AsyncSession, shop_id: int) -> None:
    order_ids = select(Order.id).where(Order.shop_id == shop_id)
    await session.execute(delete(OrderPayment).where(OrderPayment.order_id.in_(order_ids)))
    await session.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
    await session.execute(delete(Order).where(Order.shop_id == shop_id))
    await session.execute(delete(Shop).where(Shop.id == shop_id))
    await session.commit()


@router.message(Command("dashboard"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def dashboard_command(message: Message, session: AsyncSession) -> None:
    try:
        orders = await dashboard_orders(session)
        if not orders:
            await respond_to_message(message, "No dashboard orders for today.", reply_markup=dashboard_empty_keyboard())
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


@router.message(Command("shops"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def shops_command(message: Message, session: AsyncSession) -> None:
    shops = await all_shops(session)
    if not shops:
        await respond_to_message(message, "🏪 <b>Магазины</b>\n\nСписок магазинов пуст.", parse_mode="HTML")
        return
    await respond_to_message(message, "🏪 <b>Магазины</b>", reply_markup=shops_keyboard(shops), parse_mode="HTML")


@router.callback_query(F.data == "shops:list")
async def shops_list(callback: CallbackQuery, session: AsyncSession) -> None:
    await render_shops_list(callback, session)
    await callback.answer()


@router.callback_query(F.data == "shops:dashboard")
async def shops_back_to_dashboard(callback: CallbackQuery, session: AsyncSession) -> None:
    await render_dashboard(callback, session)
    await callback.answer()


@router.callback_query(F.data.startswith("shops:open:"))
async def shop_open(callback: CallbackQuery, session: AsyncSession) -> None:
    raw_shop_id = callback.data.rsplit(":", 1)[1]
    try:
        shop_id = int(raw_shop_id)
    except ValueError:
        await callback.answer("Invalid shop ID.", show_alert=True)
        return

    shop = await session.get(Shop, shop_id)
    if shop is None:
        await callback.answer("Магазин не найден.", show_alert=True)
        await render_shops_list(callback, session)
        return

    await callback.message.edit_text(
        await shop_details_text(session, shop),
        reply_markup=shop_details_keyboard(shop.id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shops:delete:"))
async def shop_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    raw_shop_id = callback.data.rsplit(":", 1)[1]
    try:
        shop_id = int(raw_shop_id)
    except ValueError:
        await callback.answer("Invalid shop ID.", show_alert=True)
        return

    shop = await session.get(Shop, shop_id)
    if shop is None:
        await callback.answer("Магазин не найден.", show_alert=True)
        await render_shops_list(callback, session)
        return

    await callback.message.edit_text(
        f"⚠️ Вы уверены, что хотите удалить магазин <b>{escape(shop.name)}</b>? "
        "Все связанные с ним заказы также будут затронуты!",
        reply_markup=shop_delete_confirmation_keyboard(shop.id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shops:cancel_delete:"))
async def shop_cancel_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    raw_shop_id = callback.data.rsplit(":", 1)[1]
    try:
        shop_id = int(raw_shop_id)
    except ValueError:
        await callback.answer("Invalid shop ID.", show_alert=True)
        return

    shop = await session.get(Shop, shop_id)
    if shop is None:
        await callback.answer("Магазин не найден.", show_alert=True)
        await render_shops_list(callback, session)
        return

    await callback.message.edit_text(
        await shop_details_text(session, shop),
        reply_markup=shop_details_keyboard(shop.id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shops:confirm_delete:"))
async def shop_confirm_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    raw_shop_id = callback.data.rsplit(":", 1)[1]
    try:
        shop_id = int(raw_shop_id)
    except ValueError:
        await callback.answer("Invalid shop ID.", show_alert=True)
        return

    shop = await session.get(Shop, shop_id)
    if shop is None:
        await callback.answer("Магазин не найден.", show_alert=True)
        await render_shops_list(callback, session)
        return

    shop_name = shop.name
    try:
        await delete_shop_with_orders(session, shop.id)
    except Exception:
        logging.exception("Shop deletion failed")
        await session.rollback()
        await callback.answer("Ошибка удаления магазина.", show_alert=True)
        return

    await render_shops_list(callback, session, prefix=f"Магазин {shop_name} успешно удален!")
    await callback.answer(f"Магазин {shop_name} успешно удален!", show_alert=True)


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
