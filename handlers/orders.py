import re
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryStatus, Shop
from services.orders import (
    add_payment,
    all_shops,
    create_order_from_parsed,
    dashboard_orders,
    dashboard_day_bounds,
    get_or_create_shop,
    get_order,
    item_subtotal,
    item_unit_price,
    match_existing_shop_name,
    paid_amount,
    update_item_unit_price,
    remaining_amount,
    top_shops,
)
from services.parser import parse_order_text

router = Router()
ORDER_CHAT_TYPES = (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP)


async def respond_to_message(message: Message, text: str, **kwargs):
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await message.reply(text, **kwargs)
    return await message.answer(text, **kwargs)


def looks_like_order_text(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    has_product = any(word in low for word in ("gummy", "gummies", "гамми", "гамме", "brownie", "брауни", "cookie", "cookies"))
    has_amount = bool(re.search(r"\d+\s*(?:mg|мг|g|гр|г|pcs?|шт|пач|x)", low))
    return has_product and has_amount


class OrderFlow(StatesGroup):
    choosing_shop = State()
    entering_shop_name = State()
    entering_shop_address = State()
    entering_split_amount = State()
    choosing_payment_method = State()
    entering_tracking = State()
    entering_custom_price = State()


def money(value: Decimal | int | str | None) -> str:
    if value is None:
        return "0"
    amount = Decimal(str(value))
    return f"{amount:,.2f}".rstrip("0").rstrip(".")


def payment_label(order) -> str:
    if order.payment_status.value == "paid":
        return f"✅ Paid ({money(paid_amount(order))} THB)"
    if order.payment_status.value == "partially_paid":
        return f"🟡 Partially Paid: {money(paid_amount(order))} / {money(order.total_amount)} THB"
    return "⏳ Processing"


def delivery_label(order) -> str:
    if order.delivery_status == DeliveryStatus.delivered:
        return "✅ Delivered"
    if order.delivery_status == DeliveryStatus.shipped:
        tracking = f" ({escape(order.tracking_number)})" if order.tracking_number else ""
        return f"📦 Shipped{tracking}"
    return "⏳ Pending Shipment"


def product_display_name(product) -> str:
    return escape(" ".join(str(product.name).split()).title())


def order_card_text(order) -> str:
    lines = [
        f"📦 <b>Order # {order.id}</b>",
        f"🏪 Shop: <b>{escape(order.shop.name)}</b>",
        f"📍 Address: {escape(order.shop.address) if order.shop.address else 'not specified'}",
        "",
        "🛍 <b>Items:</b>",
    ]
    for item in order.items:
        product = item.product
        if item.is_gift:
            details = []
            if product.flavor:
                details.append(f"({escape(product.flavor)})")
            if product.dosage:
                details.append(f"{product.dosage}mg")
            suffix = f" {' '.join(details)}" if details else ""
            lines.append(f"• {product_display_name(product)}{suffix} (Gift 🎁) — {item.quantity} pcs = <b>0 THB</b>")
            continue

        flavor = f" ({escape(product.flavor)})" if product.flavor else ""
        dosage = f" {product.dosage}mg" if product.dosage else ""
        unit_price = item_unit_price(item)
        subtotal = item_subtotal(item)
        lines.append(
            f"• {product_display_name(product)}{flavor}{dosage} — "
            f"{item.quantity} pcs x {money(unit_price)} THB = <b>{money(subtotal)} THB</b>"
        )
    lines.extend(
        [
            "",
            f"💵 <b>Total Amount: {money(order.total_amount)} THB</b>",
            f"💳 Payment: {payment_label(order)}",
            f"🚚 Delivery: {delivery_label(order)}",
            "",
            f"Paid: {money(paid_amount(order))} THB",
            f"Remaining: {money(remaining_amount(order))} THB",
        ]
    )
    return "\n".join(lines)


def order_card_keyboard(order_id: int, delivered: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if not delivered:
        rows.append(
            [
                InlineKeyboardButton(text="Edit Payment", callback_data=f"pay:{order_id}"),
                InlineKeyboardButton(text="Edit Delivery", callback_data=f"del:{order_id}"),
            ]
        )
        rows.append([InlineKeyboardButton(text="✏️ Изменить цену / Edit Prices", callback_data=f"pr:{order_id}")])
    rows.append([InlineKeyboardButton(text="Dashboard", callback_data="dash")])
    rows.append([InlineKeyboardButton(text="❌ Удалить заказ / Delete Order", callback_data=f"delete_order:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shops_keyboard(shops) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=shop.name[:40], callback_data=f"shop:{shop.id}")] for shop in shops]
    rows.append([InlineKeyboardButton(text="New/Search Shop", callback_data="shop:new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Paid in Full", callback_data=f"pf:{order_id}")],
            [InlineKeyboardButton(text="Split Payment", callback_data=f"ps:{order_id}")],
            [InlineKeyboardButton(text="Back", callback_data=f"ord:{order_id}")],
        ]
    )


def method_keyboard(order_id: int, mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Cash", callback_data=f"pm:{mode}:{order_id}:cash"),
                InlineKeyboardButton(text="Transaction", callback_data=f"pm:{mode}:{order_id}:transaction"),
            ],
            [InlineKeyboardButton(text="Crypto", callback_data=f"pm:{mode}:{order_id}:crypto")],
        ]
    )


def delivery_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Shipped", callback_data=f"ship:{order_id}"),
                InlineKeyboardButton(text="Delivered", callback_data=f"done:{order_id}"),
            ],
            [InlineKeyboardButton(text="Back", callback_data=f"ord:{order_id}")],
        ]
    )


def edit_prices_keyboard(order) -> InlineKeyboardMarkup:
    rows = []
    for index, item in enumerate(order.items, start=1):
        product = item.product
        bits = [f"{index}.", product.name.title()]
        if product.flavor:
            bits.append(str(product.flavor))
        if product.dosage:
            bits.append(f"{product.dosage}mg")
        bits.append(f"- {money(item_unit_price(item))} THB")
        rows.append([InlineKeyboardButton(text=" ".join(bits)[:60], callback_data=f"pi:{order.id}:{item.id}")])
    rows.append([InlineKeyboardButton(text="Back", callback_data=f"ord:{order.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_order_card(target: Message, session: AsyncSession, order_id: int) -> None:
    order = await get_order(session, order_id)
    delivered = order.delivery_status == DeliveryStatus.delivered
    await target.edit_text(
        order_card_text(order), reply_markup=order_card_keyboard(order.id, delivered=delivered), parse_mode="HTML"
    )


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


def dashboard_keyboard(orders) -> InlineKeyboardMarkup:
    rows = []
    for order in orders:
        text = (
            f"#{order.id} {order.shop.name[:24]} | "
            f"{order.delivery_status.value} | {order.payment_status.value}"
        )
        rows.append([InlineKeyboardButton(text=text[:64], callback_data=f"ord:{order.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def start(message: Message) -> None:
    await respond_to_message(message, "Send or forward a raw order message. Use /dashboard to review active orders.")


@router.message(Command("dashboard"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def dashboard(message: Message, session: AsyncSession) -> None:
    orders = await dashboard_orders(session)
    if not orders:
        await respond_to_message(message, "No dashboard orders for today.")
        return
    await respond_to_message(message, dashboard_summary_text(orders), reply_markup=dashboard_keyboard(orders), parse_mode="HTML")


@router.callback_query(F.data == "dash")
async def dashboard_cb(callback: CallbackQuery, session: AsyncSession) -> None:
    orders = await dashboard_orders(session)
    if not orders:
        await callback.message.edit_text("No dashboard orders for today.")
        await callback.answer()
        return
    await callback.message.edit_text(
        dashboard_summary_text(orders), reply_markup=dashboard_keyboard(orders), parse_mode="HTML"
    )
    await callback.answer()


@router.message(StateFilter(None), F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def parse_new_order(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await process_order_text(message, state, session)


async def process_order_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    shops = await all_shops(session)
    parsed = await parse_order_text(message.text, [shop.name for shop in shops])
    if not parsed.get("items"):
        await state.clear()
        await respond_to_message(message, "I could not find order items in that message.")
        return
    await state.set_data({"parsed": parsed})
    shop_name = parsed.get("shop_name")
    if shop_name:
        matched_shop = match_existing_shop_name(shop_name, shops)
        if matched_shop:
            parsed["shop_name"] = matched_shop.name
            order = await create_order_from_parsed(session, parsed, matched_shop, message.from_user.id)
            await state.clear()
            await respond_to_message(message, order_card_text(order), reply_markup=order_card_keyboard(order.id), parse_mode="HTML")
            return
        await state.update_data(shop_name=shop_name)
        await state.set_state(OrderFlow.entering_shop_address)
        await respond_to_message(
            message,
            f"Shop '{escape(shop_name)}' not found. Create it?\nType the physical address to create this shop.",
            parse_mode="HTML",
        )
        return
    shops = await top_shops(session)
    await state.set_state(OrderFlow.choosing_shop)
    await respond_to_message(message, "Choose a shop for this order:", reply_markup=shops_keyboard(shops))


@router.message(OrderFlow.choosing_shop, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def replace_draft_while_choosing_shop(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await process_order_text(message, state, session)


@router.callback_query(F.data.startswith("shop:"))
async def choose_shop(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    choice = callback.data.split(":", 1)[1]
    if choice == "new":
        await state.set_state(OrderFlow.entering_shop_name)
        await callback.message.edit_text("Type the shop name.")
        await callback.answer()
        return
    data = await state.get_data()
    parsed = data.get("parsed")
    if not parsed:
        await callback.message.edit_text("Order draft expired. Send the raw order again.")
        await state.clear()
        await callback.answer()
        return
    from sqlalchemy import select

    selected = await session.scalar(select(Shop).where(Shop.id == int(choice)))
    if not selected:
        await callback.message.edit_text("Shop not found. Send the raw order again.")
        await state.clear()
        await callback.answer()
        return
    order = await create_order_from_parsed(session, parsed, selected, callback.from_user.id)
    await state.clear()
    await callback.message.edit_text(order_card_text(order), reply_markup=order_card_keyboard(order.id), parse_mode="HTML")
    await callback.answer()


@router.message(OrderFlow.entering_shop_name, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def enter_shop_name(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if looks_like_order_text(message.text):
        await process_order_text(message, state, session)
        return
    await state.update_data(shop_name=message.text.strip())
    await state.set_state(OrderFlow.entering_shop_address)
    await respond_to_message(message, "Type the physical address.")


@router.message(OrderFlow.entering_shop_address, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def enter_shop_address(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if looks_like_order_text(message.text):
        await process_order_text(message, state, session)
        return
    data = await state.get_data()
    parsed = data.get("parsed")
    if not parsed:
        await respond_to_message(message, "Order draft expired. Send the raw order again.")
        await state.clear()
        return
    shop = await get_or_create_shop(session, data["shop_name"], message.text.strip())
    order = await create_order_from_parsed(session, parsed, shop, message.from_user.id)
    await state.clear()
    await respond_to_message(message, order_card_text(order), reply_markup=order_card_keyboard(order.id), parse_mode="HTML")


@router.callback_query(F.data.startswith("ord:"))
async def open_order(callback: CallbackQuery, session: AsyncSession) -> None:
    await show_order_card(callback.message, session, int(callback.data.split(":")[1]))
    await callback.answer()


@router.callback_query(F.data.startswith("pay:"))
async def edit_payment(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("Payment options:", reply_markup=payment_keyboard(order_id))
    await callback.answer()


@router.callback_query(F.data.startswith("pf:"))
async def paid_full(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("Choose payment method:", reply_markup=method_keyboard(order_id, "full"))
    await callback.answer()


@router.callback_query(F.data.startswith("ps:"))
async def split_payment(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = int(callback.data.split(":")[1])
    await state.update_data(order_id=order_id)
    await state.set_state(OrderFlow.entering_split_amount)
    await callback.message.edit_text("Type payment amount.")
    await callback.answer()


@router.message(OrderFlow.entering_split_amount, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def enter_split_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = Decimal(message.text.replace(",", "").strip())
        if amount <= 0:
            raise InvalidOperation
    except Exception:
        await respond_to_message(message, "Enter a positive numeric amount.")
        return
    data = await state.get_data()
    await state.update_data(amount=str(amount))
    await state.set_state(OrderFlow.choosing_payment_method)
    await respond_to_message(message, "Choose method:", reply_markup=method_keyboard(int(data["order_id"]), "split"))


@router.callback_query(F.data.startswith("pm:"))
async def choose_payment_method(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    _, mode, raw_order_id, method = callback.data.split(":")
    order = await get_order(session, int(raw_order_id))
    if mode == "full":
        amount = remaining_amount(order) if order.total_amount else Decimal("0")
    else:
        data = await state.get_data()
        amount = Decimal(data.get("amount", "0"))
    if amount <= 0:
        await callback.message.edit_text("No payable amount remains.", reply_markup=order_card_keyboard(order.id))
        await state.clear()
        await callback.answer()
        return
    order = await add_payment(session, order, method, amount)
    await callback.message.edit_text(order_card_text(order), reply_markup=order_card_keyboard(order.id), parse_mode="HTML")
    await state.clear()
    await callback.answer()


@router.callback_query(F.data.startswith("del:"))
async def edit_delivery(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("Delivery options:", reply_markup=delivery_keyboard(order_id))
    await callback.answer()


@router.callback_query(F.data.startswith("delete_order:"))
async def delete_order(callback: CallbackQuery, session: AsyncSession) -> None:
    raw_order_id = callback.data.split(":", 1)[1]
    try:
        order_id = int(raw_order_id)
    except ValueError:
        await callback.answer("Invalid order ID.", show_alert=True)
        return

    try:
        order = await get_order(session, order_id)
    except ValueError:
        await callback.answer("Order not found.", show_alert=True)
        return

    await session.delete(order)
    await session.flush()
    await callback.message.delete()
    await callback.answer("Заказ успешно удален", show_alert=False)


@router.callback_query(F.data.startswith("pr:"))
async def edit_prices(callback: CallbackQuery, session: AsyncSession) -> None:
    order_id = int(callback.data.split(":")[1])
    order = await get_order(session, order_id)
    await callback.message.edit_text("Select item to edit price:", reply_markup=edit_prices_keyboard(order))
    await callback.answer()


@router.callback_query(F.data.startswith("pi:"))
async def choose_price_item(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    _, raw_order_id, raw_item_id = callback.data.split(":")
    order = await get_order(session, int(raw_order_id))
    item_id = int(raw_item_id)
    item = next((order_item for order_item in order.items if order_item.id == item_id), None)
    if item is None:
        await callback.message.edit_text("Order item not found.", reply_markup=order_card_keyboard(order.id))
        await callback.answer()
        return
    await state.update_data(order_id=order.id, item_id=item.id)
    await state.set_state(OrderFlow.entering_custom_price)
    product = item.product
    label = product.name.title()
    if product.dosage:
        label += f" {product.dosage}mg"
    await callback.message.edit_text(
        f"Enter custom price per unit for this item (in THB):\n{escape(label)}",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(OrderFlow.entering_custom_price, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def enter_custom_price(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        price = Decimal(message.text.replace(",", "").strip())
        if price < 0:
            raise InvalidOperation
    except Exception:
        await respond_to_message(message, "Enter a non-negative numeric price in THB.")
        return
    data = await state.get_data()
    order = await update_item_unit_price(session, int(data["order_id"]), int(data["item_id"]), price)
    await respond_to_message(message, order_card_text(order), reply_markup=order_card_keyboard(order.id), parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data.startswith("ship:"))
async def mark_shipped(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = int(callback.data.split(":")[1])
    await state.update_data(order_id=order_id)
    await state.set_state(OrderFlow.entering_tracking)
    await callback.message.edit_text("Type tracking number.")
    await callback.answer()


@router.message(OrderFlow.entering_tracking, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def enter_tracking(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    order = await get_order(session, int(data["order_id"]))
    order.delivery_status = DeliveryStatus.shipped
    order.tracking_number = message.text.strip()
    await session.flush()
    order = await get_order(session, order.id)
    await respond_to_message(message, order_card_text(order), reply_markup=order_card_keyboard(order.id), parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data.startswith("done:"))
async def mark_delivered(callback: CallbackQuery, session: AsyncSession) -> None:
    order = await get_order(session, int(callback.data.split(":")[1]))
    order.delivery_status = DeliveryStatus.delivered
    await session.flush()
    order = await get_order(session, order.id)
    await callback.message.edit_text(
        order_card_text(order), reply_markup=order_card_keyboard(order.id, delivered=True), parse_mode="HTML"
    )
    await callback.answer()
