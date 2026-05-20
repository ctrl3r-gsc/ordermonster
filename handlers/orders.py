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

from db.models import DeliveryStatus, Order, PaymentStatus
from services.orders import (
    add_payment,
    all_shops,
    create_order_from_parsed,
    dashboard_has_next_page,
    dashboard_orders,
    display_order_number,
    format_dashboard_datetime,
    format_order_datetime,
    get_or_create_shop,
    get_order,
    item_subtotal,
    item_unit_price,
    match_existing_shop_name,
    paid_amount,
    set_order_payment_status,
    update_item_unit_price,
    remaining_amount,
    sanitize_shop_name,
)
from services.parser import parse_order_text

router = Router()
ORDER_CHAT_TYPES = (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP)
DASHBOARD_PAGE_SIZE = 10


async def respond_to_message(message: Message, text: str, **kwargs):
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await message.reply(text, **kwargs)
    return await message.answer(text, **kwargs)


def looks_like_order_text(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    has_product = any(
        word in low
        for word in (
            "gummy",
            "gummies",
            "guumies",
            "gumies",
            "gummys",
            "гамми",
            "гамме",
            "brownie",
            "broni",
            "брауни",
            "cookie",
            "cookies",
        )
    )
    has_amount = bool(re.search(r"\d+\s*(?:mg|мг|g|гр|г|pcs?|шт|пач|x)", low))
    return has_product and has_amount


class OrderFlow(StatesGroup):
    setting_shop = State()
    adding_address = State()
    adding_phone = State()
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
        method = ""
        if order.payments:
            method = f" ({order.payments[-1].payment_method.value.title()})"
        return f"✅ Paid{method} ({money(paid_amount(order))} THB)"
    if order.payment_status.value == "partially_paid":
        method = ""
        if order.payments:
            method = f" via {order.payments[-1].payment_method.value.title()}"
        return f"🟡 Partially Paid: {money(paid_amount(order))} / {money(order.total_amount)} THB{method}"
    return "⏳ Processing"


def delivery_label(order) -> str:
    if order.delivery_status == DeliveryStatus.delivered:
        return "✅ Delivered"
    if order.delivery_status == DeliveryStatus.shipped:
        tracking = f" ({escape(order.tracking_number)})" if order.tracking_number else ""
        return f"📦 Shipped{tracking}"
    return "⏳ Pending Shipment"


def product_display_name(product) -> str:
    return escape(" ".join(str(product.name).split()))


def order_card_text(order, parsed_address: str | None = None, parsed_phone: str | None = None) -> str:
    address = parsed_address if parsed_address else order.shop.address or ""
    phone = parsed_phone if parsed_phone else order.shop.phone_number
    lines = [
        f"📦 <b>Order # {display_order_number(order)}</b>",
        f"📅 Date: {format_order_datetime(order.created_at)}",
        f"🏪 Shop: <b>{escape(order.shop.name)}</b>",
        f"📍 Address: {escape(address)}",
    ]
    if phone:
        lines.append(f"📱 Mobile: {escape(phone)}")
    lines.extend(["", "🛍️ <b>Items:</b>"])
    for item in order.items:
        product = item.product
        if item.is_gift:
            details = []
            if product.flavor:
                details.append(f"({escape(product.flavor)})")
            suffix = f" {' '.join(details)}" if details else ""
            lines.append(f"• {product_display_name(product)}{suffix} (Gift 🎁) — {item.quantity} pcs = <b>0 THB</b>")
            continue

        flavor = f" ({escape(product.flavor)})" if product.flavor else ""
        unit_price = item_unit_price(item)
        subtotal = item_subtotal(item)
        lines.append(
            f"• {product_display_name(product)}{flavor} — "
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


def order_card_keyboard(order_or_id, delivered: bool = False) -> InlineKeyboardMarkup:
    order = order_or_id if hasattr(order_or_id, "id") else None
    order_id = order.id if order else int(order_or_id)
    is_delivered = order.delivery_status == DeliveryStatus.delivered if order else delivered
    rows = [[InlineKeyboardButton(text="🔄 Change Payment Status", callback_data=f"pay_status:{order_id}")]]
    if not is_delivered:
        rows.append([InlineKeyboardButton(text="Edit Delivery", callback_data=f"del:{order_id}")])
    rows.append([InlineKeyboardButton(text="Edit Prices", callback_data=f"pr:{order_id}")])
    if order:
        missing_row = []
        if not order.shop.address:
            missing_row.append(InlineKeyboardButton(text="📍 Add Address", callback_data=f"add_addr:{order_id}"))
        if not order.shop.phone_number:
            missing_row.append(InlineKeyboardButton(text="📱 Add Phone", callback_data=f"add_phone:{order_id}"))
        if missing_row:
            rows.append(missing_row)
    rows.append([InlineKeyboardButton(text="Dashboard", callback_data="dash")])
    rows.append([InlineKeyboardButton(text="🗑 Delete Order", callback_data=f"delete_order:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def draft_order_card_text(parsed: dict) -> str:
    lines = [
        "📦 <b>Draft Order</b>",
        "🏪 Shop: <b>missing</b>",
    ]
    address = (parsed.get("address") or "").strip()
    phone = (parsed.get("phone_number") or "").strip()
    if address:
        lines.append(f"📍 Address: {escape(address)}")
    if phone:
        lines.append(f"📱 Mobile: {escape(phone)}")
    lines.extend(["", "🛍️ <b>Items:</b>"])
    for item in parsed.get("items", []):
        product_name = escape(str(item.get("product_name") or "Item"))
        quantity = int(item.get("quantity") or 1)
        lines.append(f"• {product_name} — {quantity} pcs")
    return "\n".join(lines)


def draft_order_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏪 Set Shop", callback_data="draft:set_shop")],
        ]
    )


def delete_confirmation_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yes, Delete", callback_data=f"confirm_del:{order_id}")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_del:{order_id}")],
        ]
    )


def payment_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Paid in Full", callback_data=f"pf:{order_id}")],
            [InlineKeyboardButton(text="Split Payment", callback_data=f"ps:{order_id}")],
            [InlineKeyboardButton(text="Back", callback_data=f"ord:{order_id}")],
        ]
    )


def payment_status_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💵 Mark as Cash", callback_data=f"pay_status_set:{order_id}:cash")],
            [InlineKeyboardButton(text="💳 Mark as Transfer", callback_data=f"pay_status_set:{order_id}:transaction")],
            [InlineKeyboardButton(text="🪙 Mark as Crypto", callback_data=f"pay_status_set:{order_id}:crypto")],
            [InlineKeyboardButton(text="⏳ Reset to Processing (Unpaid)", callback_data=f"pay_status_set:{order_id}:unpaid")],
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
        bits = [f"{index}.", product.name]
        if product.flavor:
            bits.append(str(product.flavor))
        bits.append(f"- {money(item_unit_price(item))} THB")
        rows.append([InlineKeyboardButton(text=" ".join(bits)[:60], callback_data=f"pi:{order.id}:{item.id}")])
    rows.append([InlineKeyboardButton(text="Back", callback_data=f"ord:{order.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_order_card(target: Message, session: AsyncSession, order_id: int) -> None:
    order = await get_order(session, order_id)
    await target.edit_text(
        order_card_text(order), reply_markup=order_card_keyboard(order), parse_mode="HTML"
    )


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


def dashboard_order_state_emoji(order) -> str:
    if order.delivery_status == DeliveryStatus.delivered and order.payment_status.value == "paid":
        return "✅"
    return "⌛"


def dashboard_keyboard(orders, page: int = 0, has_next: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for order in orders:
        created_at = format_dashboard_datetime(order.created_at).replace(" ", " (") + ")"
        text = (
            f"#{display_order_number(order)} | 📅 {created_at} | {order.shop.name[:18]} | "
            f"{dashboard_order_state_emoji(order)}"
        )
        rows.append([InlineKeyboardButton(text=text[:64], callback_data=f"ord:{order.id}")])
    pagination_row = []
    if page > 0:
        pagination_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"dash_page:{page - 1}"))
    if has_next:
        pagination_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"dash_page:{page + 1}"))
    if pagination_row:
        rows.append(pagination_row)
    rows.append([InlineKeyboardButton(text="🏪 Shops", callback_data="shops:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dashboard_empty_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏪 Shops", callback_data="shops:list")]])


@router.message(Command("start"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def start(message: Message) -> None:
    await respond_to_message(message, "Send or forward a raw order message. Use /dashboard to review active orders.")


@router.callback_query(F.data == "dash")
async def dashboard_cb(callback: CallbackQuery, session: AsyncSession) -> None:
    orders = await dashboard_orders(session, page=0, limit=DASHBOARD_PAGE_SIZE)
    has_next = await dashboard_has_next_page(session, page=0, limit=DASHBOARD_PAGE_SIZE)
    if not orders:
        await callback.message.edit_text("No dashboard orders found.", reply_markup=dashboard_empty_keyboard())
        await callback.answer()
        return
    await callback.message.edit_text(
        dashboard_summary_text(orders, page=0),
        reply_markup=dashboard_keyboard(orders, page=0, has_next=has_next),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"), F.chat.type.in_(ORDER_CHAT_TYPES))
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
    shop_name = (parsed.get("shop_name") or "").upper().strip()
    shop_name = sanitize_shop_name(shop_name)
    if shop_name:
        parsed["shop_name"] = shop_name
        matched_shop = match_existing_shop_name(shop_name, shops)
        shop = matched_shop or await get_or_create_shop(
            session,
            shop_name,
            parsed.get("address"),
            parsed.get("phone_number"),
        )
        if matched_shop:
            parsed["shop_name"] = matched_shop.name
        order = await create_order_from_parsed(session, parsed, shop, message.from_user.id)
        await state.clear()
        await respond_to_message(
            message,
            order_card_text(order),
            reply_markup=order_card_keyboard(order),
            parse_mode="HTML",
        )
        return
    await respond_to_message(
        message,
        draft_order_card_text(parsed),
        reply_markup=draft_order_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "draft:set_shop")
async def draft_set_shop(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("parsed"):
        await callback.answer("Order draft expired. Send the order again.", show_alert=True)
        return
    await state.set_state(OrderFlow.setting_shop)
    await callback.message.edit_text("Type the shop name for this order.")
    await callback.answer()


@router.message(OrderFlow.setting_shop, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def set_draft_shop(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    parsed = data.get("parsed")
    if not parsed:
        await respond_to_message(message, "Order draft expired. Send the order again.")
        await state.clear()
        return
    shop_name = sanitize_shop_name((message.text or "").upper().strip())
    if not shop_name:
        await respond_to_message(message, "Type a valid shop name.")
        return
    parsed["shop_name"] = shop_name
    shop = await get_or_create_shop(session, shop_name, parsed.get("address"), parsed.get("phone_number"))
    order = await create_order_from_parsed(session, parsed, shop, message.from_user.id)
    await state.clear()
    await respond_to_message(
        message,
        order_card_text(order),
        reply_markup=order_card_keyboard(order),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("add_addr:"))
async def add_address(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = int(callback.data.split(":")[1])
    await state.update_data(order_id=order_id)
    await state.set_state(OrderFlow.adding_address)
    await callback.message.edit_text("Type the address for this shop.")
    await callback.answer()


@router.message(OrderFlow.adding_address, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def enter_added_address(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    order = await get_order(session, int(data["order_id"]))
    order.shop.address = message.text.strip()
    await session.commit()
    order = await get_order(session, order.id)
    await state.clear()
    await respond_to_message(
        message,
        order_card_text(order),
        reply_markup=order_card_keyboard(order),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("add_phone:"))
async def add_phone(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = int(callback.data.split(":")[1])
    await state.update_data(order_id=order_id)
    await state.set_state(OrderFlow.adding_phone)
    await callback.message.edit_text("Type the mobile number for this shop.")
    await callback.answer()


@router.message(OrderFlow.adding_phone, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def enter_added_phone(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    order = await get_order(session, int(data["order_id"]))
    order.shop.phone_number = message.text.strip()
    await session.commit()
    order = await get_order(session, order.id)
    await state.clear()
    await respond_to_message(
        message,
        order_card_text(order),
        reply_markup=order_card_keyboard(order),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("ord:"))
async def open_order(callback: CallbackQuery, session: AsyncSession) -> None:
    await show_order_card(callback.message, session, int(callback.data.split(":")[1]))
    await callback.answer()


@router.callback_query(F.data.startswith("pay_status:"))
async def edit_payment_status(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("Change payment status:", reply_markup=payment_status_keyboard(order_id))
    await callback.answer()


@router.callback_query(F.data.startswith("pay_status_set:"))
async def choose_payment_status(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_order_id, status = callback.data.split(":")
    order = await get_order(session, int(raw_order_id))
    method = None if status == "unpaid" else status
    updated_order = await set_order_payment_status(session, order, method)
    await callback.message.edit_text(
        order_card_text(updated_order),
        reply_markup=order_card_keyboard(updated_order),
        parse_mode="HTML",
    )
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
        await show_order_card(callback.message, session, order.id)
        await state.clear()
        await callback.answer()
        return
    updated_order = await add_payment(session, order, method, amount)
    updated_order.payment_status = PaymentStatus.paid if mode == "full" else updated_order.payment_status
    await session.commit()
    await session.refresh(updated_order)
    updated_order = await get_order(session, updated_order.id)
    updated_text = order_card_text(updated_order)
    updated_markup = order_card_keyboard(updated_order)
    await callback.message.edit_text(text=updated_text, reply_markup=updated_markup, parse_mode="HTML")
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

    await callback.message.edit_text(
        f"{order_card_text(order)}\n\nDelete order #{display_order_number(order)}?",
        reply_markup=delete_confirmation_keyboard(order_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_del:"))
async def confirm_delete(callback: CallbackQuery, session: AsyncSession) -> None:
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

    await session.delete(order)
    await session.flush()

    if callback.message and callback.message.text and callback.message.text.lstrip().startswith("📦"):
        await callback.message.delete()
    else:
        orders = await dashboard_orders(session, page=0, limit=DASHBOARD_PAGE_SIZE)
        has_next = await dashboard_has_next_page(session, page=0, limit=DASHBOARD_PAGE_SIZE)
        if not orders:
            await callback.message.edit_text("No dashboard orders found.")
        else:
            await callback.message.edit_text(
                dashboard_summary_text(orders, page=0),
                reply_markup=dashboard_keyboard(orders, page=0, has_next=has_next),
                parse_mode="HTML",
            )

    await callback.answer(f"Order #{display_order_number(order)} deleted")


@router.callback_query(F.data.startswith("cancel_del:"))
async def cancel_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    raw_order_id = callback.data.split(":", 1)[1]
    try:
        order_id = int(raw_order_id)
    except ValueError:
        await callback.answer("Invalid order ID.", show_alert=True)
        return

    if callback.message and callback.message.text and callback.message.text.lstrip().startswith("📦"):
        await show_order_card(callback.message, session, order_id)
    else:
        orders = await dashboard_orders(session, page=0, limit=DASHBOARD_PAGE_SIZE)
        has_next = await dashboard_has_next_page(session, page=0, limit=DASHBOARD_PAGE_SIZE)
        if not orders:
            await callback.message.edit_text("No dashboard orders found.")
        else:
            await callback.message.edit_text(
                dashboard_summary_text(orders, page=0),
                reply_markup=dashboard_keyboard(orders, page=0, has_next=has_next),
                parse_mode="HTML",
            )

    await callback.answer()


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
        await callback.message.edit_text("Order item not found.", reply_markup=order_card_keyboard(order))
        await callback.answer()
        return
    await state.update_data(order_id=order.id, item_id=item.id)
    await state.set_state(OrderFlow.entering_custom_price)
    product = item.product
    label = product.name
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
    await respond_to_message(message, order_card_text(order), reply_markup=order_card_keyboard(order), parse_mode="HTML")
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
    await respond_to_message(message, order_card_text(order), reply_markup=order_card_keyboard(order), parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data.startswith("done:"))
async def mark_delivered(callback: CallbackQuery, session: AsyncSession) -> None:
    order = await get_order(session, int(callback.data.split(":")[1]))
    order.delivery_status = DeliveryStatus.delivered
    await session.flush()
    order = await get_order(session, order.id)
    await callback.message.edit_text(
        order_card_text(order), reply_markup=order_card_keyboard(order), parse_mode="HTML"
    )
    await callback.answer()
