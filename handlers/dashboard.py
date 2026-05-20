import logging
import re
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryStatus, Order, OrderItem, OrderPayment, Shop
from handlers.orders import display_order_number, order_card_keyboard, order_card_text
from services.orders import all_shops, dashboard_has_next_page, dashboard_orders, format_dashboard_datetime, get_order, sanitize_shop_input

router = Router()
ORDER_CHAT_TYPES = (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP)


class ShopFlow(StatesGroup):
    editing_address = State()


async def respond_to_message(message: Message, text: str, **kwargs):
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await message.reply(text, **kwargs)
    return await message.answer(text, **kwargs)


DASHBOARD_PAGE_SIZE = 10


def dashboard_summary_text(orders, page: int = 0) -> str:
    pending_deliveries = sum(1 for order in orders if order.delivery_status != DeliveryStatus.delivered)
    processing_payments = sum(1 for order in orders if order.payment_status.value != "paid")
    return "\n".join(
        [
            "<b>Dashboard</b>",
            f"Page: <b>{page + 1}</b>",
            f"Pending deliveries: <b>{pending_deliveries}</b>",
            f"Processing payments: <b>{processing_payments}</b>",
            "",
            "Latest orders:",
        ]
    )


def order_state_emoji(order) -> str:
    if order.delivery_status == DeliveryStatus.delivered and order.payment_status.value == "paid":
        return "✅"
    return "⌛"


def dashboard_button_text(order) -> str:
    created_at = format_dashboard_datetime(order.created_at).replace(" ", " (") + ")"
    shop_name = order.shop.name[:18]
    return f"#{display_order_number(order)} | 📅 {created_at} | {shop_name} | {order_state_emoji(order)}"[:64]


def dashboard_keyboard(orders, page: int = 0, has_next: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=dashboard_button_text(order), callback_data=f"dash_order:{order.id}")]
        for order in orders
    ]
    pagination_row = []
    if page > 0:
        pagination_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"dash_page:{page - 1}"))
    if has_next:
        pagination_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"dash_page:{page + 1}"))
    if pagination_row:
        rows.append(pagination_row)
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
            [InlineKeyboardButton(text="📍 Add/Edit Address", callback_data=f"shops:edit_address:{shop_id}")],
            [InlineKeyboardButton(text="🗑 Delete Shop", callback_data=f"shops:delete:{shop_id}")],
            [InlineKeyboardButton(text="🔙 Back to Shops", callback_data="shops:list")],
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


async def render_dashboard(callback: CallbackQuery, session: AsyncSession, page: int = 0) -> None:
    page = max(page, 0)
    orders = await dashboard_orders(session, page=page, limit=DASHBOARD_PAGE_SIZE)
    has_next = await dashboard_has_next_page(session, page=page, limit=DASHBOARD_PAGE_SIZE)
    if not orders:
        await callback.message.edit_text("No dashboard orders found.", reply_markup=dashboard_empty_keyboard())
        return
    await callback.message.edit_text(
        dashboard_summary_text(orders, page=page),
        reply_markup=dashboard_keyboard(orders, page=page, has_next=has_next),
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
        orders = await dashboard_orders(session, page=0, limit=DASHBOARD_PAGE_SIZE)
        has_next = await dashboard_has_next_page(session, page=0, limit=DASHBOARD_PAGE_SIZE)
        if not orders:
            await respond_to_message(message, "No dashboard orders found.", reply_markup=dashboard_empty_keyboard())
            return
        await respond_to_message(
            message,
            dashboard_summary_text(orders, page=0),
            reply_markup=dashboard_keyboard(orders, page=0, has_next=has_next),
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


@router.callback_query(F.data.startswith("dash_page:"))
async def dashboard_page(callback: CallbackQuery, session: AsyncSession) -> None:
    raw_page = callback.data.split(":", 1)[1]
    try:
        page = max(int(raw_page), 0)
    except ValueError:
        await callback.answer("Invalid dashboard page.", show_alert=True)
        return
    await render_dashboard(callback, session, page=page)
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


@router.callback_query(F.data.startswith("shops:edit_address:"))
async def shop_edit_address(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    raw_shop_id = callback.data.rsplit(":", 1)[1]
    try:
        shop_id = int(raw_shop_id)
    except ValueError:
        await callback.answer("Invalid shop ID.", show_alert=True)
        return
    shop = await session.get(Shop, shop_id)
    if shop is None:
        await callback.answer("Shop not found.", show_alert=True)
        await render_shops_list(callback, session)
        return
    await state.update_data(shop_id=shop.id)
    await state.set_state(ShopFlow.editing_address)
    await callback.message.edit_text("Send the shop address, Google Maps link, or phone number.")
    await callback.answer()


@router.message(ShopFlow.editing_address, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def shop_save_address(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    shop = await session.get(Shop, int(data["shop_id"]))
    if shop is None:
        await respond_to_message(message, "Shop not found.")
        await state.clear()
        return
    raw_value = (message.text or "").strip()
    phone_match = re.search(r"\b\d{9,11}\b", raw_value)
    if phone_match and raw_value == phone_match.group(0):
        shop.phone_number = phone_match.group(0)
    else:
        phone_number = phone_match.group(0) if phone_match else shop.phone_number
        _, clean_address = sanitize_shop_input(shop.name, raw_value, phone_number)
        shop.address = clean_address
        if phone_match:
            shop.phone_number = phone_match.group(0)
    await session.commit()
    await state.clear()
    await respond_to_message(
        message,
        await shop_details_text(session, shop),
        reply_markup=shop_details_keyboard(shop.id),
        parse_mode="HTML",
    )


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
            reply_markup=order_card_keyboard(order, delivered=delivered),
            parse_mode="HTML",
        )
        await callback.answer()
    except Exception:
        logging.exception("Dashboard order open handler failed")
        await callback.answer("Ошибка обработки запроса.", show_alert=True)
