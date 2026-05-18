from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryStatus, Shop
from services.orders import (
    add_payment,
    create_order_from_parsed,
    dashboard_orders,
    get_or_create_shop,
    get_order,
    paid_amount,
    remaining_amount,
    top_shops,
)
from services.parser import parse_order_text

router = Router()


class OrderFlow(StatesGroup):
    choosing_shop = State()
    entering_shop_name = State()
    entering_shop_address = State()
    entering_split_amount = State()
    choosing_payment_method = State()
    entering_tracking = State()


def money(value: Decimal | int | str | None) -> str:
    if value is None:
        return "unknown"
    amount = Decimal(str(value))
    return f"{amount:,.2f}".rstrip("0").rstrip(".")


def order_card_text(order) -> str:
    lines = [
        f"Order #{order.id}",
        f"Shop: {order.shop.name}",
        f"Delivery: {order.delivery_status.value}",
        f"Payment: {order.payment_status.value}",
    ]
    if order.tracking_number:
        lines.append(f"Tracking: {order.tracking_number}")
    lines.append("")
    lines.append("Items:")
    for item in order.items:
        product = item.product
        bits = [product.name]
        if product.dosage:
            bits.append(f"{product.dosage}mg")
        if product.flavor:
            bits.append(product.flavor)
        gift = " gift" if item.is_gift else ""
        lines.append(f"- {' '.join(bits)} x{item.quantity}{gift}")
    lines.extend(
        [
            "",
            f"Total: {money(order.total_amount)}",
            f"Paid: {money(paid_amount(order))}",
            f"Remaining: {money(remaining_amount(order))}",
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
    rows.append([InlineKeyboardButton(text="Dashboard", callback_data="dash")])
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


async def show_order_card(target: Message, session: AsyncSession, order_id: int) -> None:
    order = await get_order(session, order_id)
    delivered = order.delivery_status == DeliveryStatus.delivered
    await target.edit_text(order_card_text(order), reply_markup=order_card_keyboard(order.id, delivered=delivered))


@router.message(Command("start"))
async def start(message: Message) -> None:
    await message.answer("Send or forward a raw order message. Use /dashboard to review active orders.")


@router.message(Command("dashboard"))
async def dashboard(message: Message, session: AsyncSession) -> None:
    orders = await dashboard_orders(session)
    if not orders:
        await message.answer("No active or issue orders.")
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"#{order.id} {order.shop.name[:24]} | {order.delivery_status.value} | {order.payment_status.value}",
                callback_data=f"ord:{order.id}",
            )
        ]
        for order in orders
    ]
    await message.answer("Active / issue orders:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "dash")
async def dashboard_cb(callback: CallbackQuery, session: AsyncSession) -> None:
    orders = await dashboard_orders(session)
    if not orders:
        await callback.message.edit_text("No active or issue orders.")
        await callback.answer()
        return
    rows = [
        [InlineKeyboardButton(text=f"#{order.id} {order.shop.name[:28]}", callback_data=f"ord:{order.id}")]
        for order in orders
    ]
    await callback.message.edit_text("Active / issue orders:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.message(StateFilter(None), F.text)
async def parse_new_order(message: Message, state: FSMContext, session: AsyncSession) -> None:
    parsed = await parse_order_text(message.text)
    if not parsed.get("items"):
        await message.answer("I could not find order items in that message.")
        return
    await state.update_data(parsed=parsed)
    shop_name = parsed.get("shop_name")
    if shop_name:
        shop = await get_or_create_shop(session, shop_name)
        order = await create_order_from_parsed(session, parsed, shop, message.from_user.id)
        await message.answer(order_card_text(order), reply_markup=order_card_keyboard(order.id))
        await state.clear()
        return
    shops = await top_shops(session)
    await state.set_state(OrderFlow.choosing_shop)
    await message.answer("Choose a shop for this order:", reply_markup=shops_keyboard(shops))


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
    await callback.message.edit_text(order_card_text(order), reply_markup=order_card_keyboard(order.id))
    await state.clear()
    await callback.answer()


@router.message(OrderFlow.entering_shop_name, F.text)
async def enter_shop_name(message: Message, state: FSMContext) -> None:
    await state.update_data(shop_name=message.text.strip())
    await state.set_state(OrderFlow.entering_shop_address)
    await message.answer("Type the physical address.")


@router.message(OrderFlow.entering_shop_address, F.text)
async def enter_shop_address(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    parsed = data.get("parsed")
    if not parsed:
        await message.answer("Order draft expired. Send the raw order again.")
        await state.clear()
        return
    shop = await get_or_create_shop(session, data["shop_name"], message.text.strip())
    order = await create_order_from_parsed(session, parsed, shop, message.from_user.id)
    await message.answer(order_card_text(order), reply_markup=order_card_keyboard(order.id))
    await state.clear()


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


@router.message(OrderFlow.entering_split_amount, F.text)
async def enter_split_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = Decimal(message.text.replace(",", "").strip())
        if amount <= 0:
            raise InvalidOperation
    except Exception:
        await message.answer("Enter a positive numeric amount.")
        return
    data = await state.get_data()
    await state.update_data(amount=str(amount))
    await state.set_state(OrderFlow.choosing_payment_method)
    await message.answer("Choose method:", reply_markup=method_keyboard(int(data["order_id"]), "split"))


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
    await callback.message.edit_text(order_card_text(order), reply_markup=order_card_keyboard(order.id))
    await state.clear()
    await callback.answer()


@router.callback_query(F.data.startswith("del:"))
async def edit_delivery(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("Delivery options:", reply_markup=delivery_keyboard(order_id))
    await callback.answer()


@router.callback_query(F.data.startswith("ship:"))
async def mark_shipped(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = int(callback.data.split(":")[1])
    await state.update_data(order_id=order_id)
    await state.set_state(OrderFlow.entering_tracking)
    await callback.message.edit_text("Type tracking number.")
    await callback.answer()


@router.message(OrderFlow.entering_tracking, F.text)
async def enter_tracking(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    order = await get_order(session, int(data["order_id"]))
    order.delivery_status = DeliveryStatus.shipped
    order.tracking_number = message.text.strip()
    await session.flush()
    order = await get_order(session, order.id)
    await message.answer(order_card_text(order), reply_markup=order_card_keyboard(order.id))
    await state.clear()


@router.callback_query(F.data.startswith("done:"))
async def mark_delivered(callback: CallbackQuery, session: AsyncSession) -> None:
    order = await get_order(session, int(callback.data.split(":")[1]))
    order.delivery_status = DeliveryStatus.delivered
    await session.flush()
    order = await get_order(session, order.id)
    await callback.message.edit_text(order_card_text(order), reply_markup=order_card_keyboard(order.id, delivered=True))
    await callback.answer()
