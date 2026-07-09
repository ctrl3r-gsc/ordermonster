import logging
import re
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryStatus, Order, Shop
from services.catalog import active_catalog, all_catalog, catalog_for_parser
from services.orders import (
    add_payment_to_order,
    all_shops,
    create_order_from_parsed,
    dashboard_has_next_page,
    dashboard_orders,
    dashboard_status_counts,
    display_order_number,
    format_dashboard_datetime,
    format_order_datetime,
    get_or_create_shop,
    get_order,
    get_order_with_relations,
    item_subtotal,
    item_unit_price,
    match_existing_shop_name,
    paid_amount,
    sanitize_shop_input,
    set_order_payment_status_by_id,
    update_item_unit_price,
    remaining_amount,
    sanitize_shop_name,
)
from services.notifications import send_new_order_notification
from services.parser import parse_order_text

router = Router()
ORDER_CHAT_TYPES = (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP)
DASHBOARD_PAGE_SIZE = 10
logger = logging.getLogger(__name__)


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
    selecting_product = State()
    adding_address = State()
    adding_phone = State()
    entering_split_amount = State()
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
    rows = [[InlineKeyboardButton(text="🔄 Change Payment Status", callback_data=f"pay:{order_id}")]]
    if not is_delivered:
        rows.append([InlineKeyboardButton(text="✏️ Edit Delivery", callback_data=f"del:{order_id}")])
    rows.append([InlineKeyboardButton(text="💵 Edit Prices", callback_data=f"pr:{order_id}")])
    if order:
        missing_row = []
        if not order.shop.address:
            missing_row.append(InlineKeyboardButton(text="📍 Add Address", callback_data=f"add_addr:{order_id}"))
        if not order.shop.phone_number:
            missing_row.append(InlineKeyboardButton(text="📞 Add Phone", callback_data=f"add_phone:{order_id}"))
        if missing_row:
            rows.append(missing_row)
    rows.append([InlineKeyboardButton(text="📊 Dashboard", callback_data="dash")])
    rows.append([InlineKeyboardButton(text="🗑 Delete Order", callback_data=f"delete_order:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def draft_order_card_text(parsed: dict) -> str:
    shop_label = escape(str(parsed.get("shop_name") or "missing"))
    lines = [
        "📦 <b>Draft Order</b>",
        f"🏪 Shop: <b>{shop_label}</b>",
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


def draft_shop_choice_keyboard(shops: list[Shop]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=shop.name[:60], callback_data=f"draft:shop:{shop.id}")]
        for shop in shops
    ]
    rows.append([InlineKeyboardButton(text="➕ Add New Shop", callback_data="draft:add_shop")])
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="draft:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def prioritized_shop_choices(shops: list[Shop], candidate_names: list[str] | None = None, limit: int = 10) -> list[Shop]:
    candidates = [sanitize_shop_name(candidate) for candidate in candidate_names or []]
    prioritized: list[Shop] = []
    seen: set[int] = set()
    for candidate in candidates:
        matched = match_existing_shop_name(candidate, shops)
        if matched and matched.id not in seen:
            prioritized.append(matched)
            seen.add(matched.id)
    for shop in shops:
        if shop.id not in seen:
            prioritized.append(shop)
            seen.add(shop.id)
        if len(prioritized) >= limit:
            break
    return prioritized


async def ask_shop_clarification(message: Message, state: FSMContext, parsed: dict, shops: list[Shop]) -> None:
    await state.set_data({"parsed": parsed})
    await respond_to_message(
        message,
        "Which shop is this order for?",
        reply_markup=draft_shop_choice_keyboard(prioritized_shop_choices(shops, parsed.get("shop_candidates"))),
    )


def product_choice_keyboard(products, product_ids: list[int], item_index: int | None = None) -> InlineKeyboardMarkup:
    by_id = {product.id: product for product in products}
    rows = []
    for product_id in product_ids:
        product = by_id.get(int(product_id))
        if not product:
            continue
        callback_data = f"pick_product:{item_index}:{product.id}" if item_index is not None else f"pick_product:{product.id}"
        rows.append([InlineKeyboardButton(text=product.name[:60], callback_data=callback_data)])
    if item_index is not None:
        rows.append([InlineKeyboardButton(text="Skip item", callback_data=f"skip_product:{item_index}")])
    rows.append([InlineKeyboardButton(text="Back", callback_data="draft:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_clarification_text(unresolved: dict) -> str:
    line_index = int(unresolved.get("line_index") or (int(unresolved.get("item_index") or 0) + 1))
    original_text = unresolved.get("original_text") or unresolved.get("raw_product_text") or "item"
    return f"Уточни товар для строки {line_index}:\n«{original_text}»"


async def ask_product_clarification(message: Message, state: FSMContext, parsed: dict, products) -> bool:
    unresolved = parsed.get("unresolved_products") or []
    if not unresolved:
        return False
    first = unresolved[0]
    item_index = int(first.get("item_index") or 0)
    similar_ids = first.get("similar_product_ids") or [product.id for product in products[:6]]
    await state.set_state(OrderFlow.selecting_product)
    await state.update_data(parsed=parsed)
    await respond_to_message(
        message,
        product_clarification_text(first),
        reply_markup=product_choice_keyboard(products, similar_ids, item_index),
    )
    return True


async def edit_product_clarification(callback: CallbackQuery, state: FSMContext, parsed: dict, products) -> bool:
    unresolved = parsed.get("unresolved_products") or []
    if not unresolved:
        return False
    first = unresolved[0]
    item_index = int(first.get("item_index") or 0)
    similar_ids = first.get("similar_product_ids") or [product.id for product in products[:6]]
    await state.set_state(OrderFlow.selecting_product)
    await state.update_data(parsed=parsed)
    await callback.message.edit_text(
        product_clarification_text(first),
        reply_markup=product_choice_keyboard(products, similar_ids, item_index),
    )
    return True


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
            [InlineKeyboardButton(text="💵 Cash", callback_data=f"pay_method:{order_id}:cash")],
            [InlineKeyboardButton(text="💳 Transfer", callback_data=f"pay_method:{order_id}:transaction")],
            [InlineKeyboardButton(text="🪙 Crypto", callback_data=f"pay_method:{order_id}:crypto")],
            [InlineKeyboardButton(text="🔙 Back", callback_data=f"ord:{order_id}")],
        ]
    )


def payment_amount_keyboard(order_id: int, method: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Fully Paid", callback_data=f"pay_apply:full:{order_id}:{method}")],
            [InlineKeyboardButton(text="🧾 Partially Paid", callback_data=f"pay_apply:partial:{order_id}:{method}")],
            [InlineKeyboardButton(text="🔙 Back", callback_data=f"pay:{order_id}")],
        ]
    )
def delivery_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Shipped", callback_data=f"ship:{order_id}"),
                InlineKeyboardButton(text="Delivered", callback_data=f"done:{order_id}"),
            ],
            [InlineKeyboardButton(text="🔙 Back", callback_data=f"ord:{order_id}")],
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
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data=f"ord:{order.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_order_card(target: Message, session: AsyncSession, order_id: int) -> None:
    order = await get_order_with_relations(session, order_id)
    await target.edit_text(
        order_card_text(order), reply_markup=order_card_keyboard(order), parse_mode="HTML"
    )


async def commit_payment_and_reload_order(session: AsyncSession, order_id: int) -> Order:
    await session.commit()
    session.expire_all()
    order = await get_order_with_relations(session, order_id)
    logger.info(
        "Fresh order reload performed after payment commit",
        extra={"order_id": order_id, "fresh_reload_performed": True},
    )
    return order


def dashboard_summary_text(orders, page: int = 0, counts: dict[str, int] | None = None) -> str:
    counts = counts or {
        "pending_deliveries": sum(1 for order in orders if order.delivery_status != DeliveryStatus.delivered),
        "processing_payments": sum(1 for order in orders if order.payment_status.value != "paid"),
        "closed_orders": sum(
            1
            for order in orders
            if order.delivery_status == DeliveryStatus.delivered and order.payment_status.value == "paid"
        ),
    }
    return "\n".join(
        [
            "<b>Dashboard</b>",
            f"Page: <b>{page + 1}</b>",
            f"Pending deliveries: <b>{counts['pending_deliveries']}</b>",
            f"Processing payments: <b>{counts['processing_payments']}</b>",
            f"Closed orders: <b>{counts['closed_orders']}</b>",
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
            f"#{display_order_number(order)} | {dashboard_order_state_emoji(order)} "
            f"{created_at} | {order.shop.name[:18]}"
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
    rows.append([InlineKeyboardButton(text="Products", callback_data="catalog:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dashboard_empty_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏪 Shops", callback_data="shops:list")],
            [InlineKeyboardButton(text="Products", callback_data="catalog:list")],
        ]
    )


@router.message(Command("start"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def start(message: Message) -> None:
    await respond_to_message(message, "Send or forward a raw order message. Use /dashboard to review active orders.")


@router.callback_query(F.data == "dash")
async def dashboard_cb(callback: CallbackQuery, session: AsyncSession) -> None:
    orders = await dashboard_orders(session, page=0, limit=DASHBOARD_PAGE_SIZE)
    has_next = await dashboard_has_next_page(session, page=0, limit=DASHBOARD_PAGE_SIZE)
    counts = await dashboard_status_counts(session)
    if not orders:
        await callback.message.edit_text("No dashboard orders found.", reply_markup=dashboard_empty_keyboard())
        await callback.answer()
        return
    await callback.message.edit_text(
        dashboard_summary_text(orders, page=0, counts=counts),
        reply_markup=dashboard_keyboard(orders, page=0, has_next=has_next),
        parse_mode="HTML",
    )
    await callback.answer()


def catalog_text(products) -> str:
    lines = ["<b>Products Catalog</b>"]
    for product in products:
        aliases = ", ".join(alias.alias for alias in product.aliases if alias.is_active) or "-"
        active = "active" if product.is_active else "inactive"
        lines.append(
            f"\n<b>{escape(product.name)}</b>\n"
            f"Price: {money(product.price)} THB | {active}\n"
            f"SKU: <code>{escape(product.sku)}</code>\n"
            f"Aliases: {escape(aliases)}"
        )
    return "\n".join(lines)


@router.message(Command("catalog"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def catalog_cmd(message: Message, session: AsyncSession) -> None:
    products = await all_catalog(session)
    await respond_to_message(message, catalog_text(products), parse_mode="HTML")


@router.callback_query(F.data == "catalog:list")
async def catalog_cb(callback: CallbackQuery, session: AsyncSession) -> None:
    products = await all_catalog(session)
    await callback.message.edit_text(catalog_text(products), parse_mode="HTML")
    await callback.answer()


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"), F.chat.type.in_(ORDER_CHAT_TYPES))
async def parse_new_order(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    await process_order_text(message, state, session, bot)


async def process_order_text(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    await state.clear()
    shops = await all_shops(session)
    products = await active_catalog(session)
    parser_catalog = catalog_for_parser(products)
    logger.debug("Active parser catalog: %s", parser_catalog)
    parsed = await parse_order_text(message.text, [shop.name for shop in shops], parser_catalog)
    if not parsed.get("items"):
        await state.clear()
        await respond_to_message(message, "I could not find order items in that message.")
        return
    if await ask_product_clarification(message, state, parsed, products):
        return
    await state.set_data({"parsed": parsed})
    if parsed.get("needs_shop_clarification"):
        await ask_shop_clarification(message, state, parsed, shops)
        return
    shop_name = (parsed.get("shop_name") or "").upper().strip()
    shop_name = sanitize_shop_name(shop_name)
    shop_name, parsed["address"] = sanitize_shop_input(shop_name, parsed.get("address"), parsed.get("phone_number"))
    shop_name = sanitize_shop_name(shop_name)
    if shop_name:
        parsed["shop_name"] = shop_name
        matched_shop = match_existing_shop_name(shop_name, shops)
        if not matched_shop:
            parsed["shop_name"] = None
            await respond_to_message(
                message,
                draft_order_card_text(parsed),
                reply_markup=draft_order_keyboard(),
                parse_mode="HTML",
            )
            return
        shop = matched_shop
        parsed["shop_name"] = matched_shop.name
        order = await create_order_from_parsed(session, parsed, shop, message.from_user.id)
        await state.clear()
        await respond_to_message(
            message,
            order_card_text(order),
            reply_markup=order_card_keyboard(order),
            parse_mode="HTML",
        )
        await send_new_order_notification(bot, session, order.id, message.from_user)
        return
    await respond_to_message(
        message,
        draft_order_card_text(parsed),
        reply_markup=draft_order_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(OrderFlow.selecting_product, F.data.startswith("pick_product:"))
async def pick_product(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) >= 3:
        item_index = int(parts[1])
        product_id = int(parts[2])
    else:
        item_index = None
        product_id = int(parts[1])
    products = await active_catalog(session)
    product = next((item for item in products if item.id == product_id), None)
    if product is None:
        await callback.answer("Product is not active.", show_alert=True)
        return
    data = await state.get_data()
    parsed = data.get("parsed")
    if not parsed:
        await callback.answer("Order draft expired. Send the order again.", show_alert=True)
        return
    items = parsed.get("items", [])
    if item_index is None:
        item_index = next((index for index, item in enumerate(items) if not item.get("product_id")), 0)
    if item_index < 0 or item_index >= len(items):
        await callback.answer("Order item expired. Send the order again.", show_alert=True)
        return
    items[item_index]["product_id"] = product.id
    items[item_index]["product_name"] = product.name
    items[item_index]["dosage"] = product.dosage
    items[item_index]["flavor"] = product.flavor
    parsed["unresolved_products"] = [
        unresolved
        for unresolved in parsed.get("unresolved_products", [])
        if int(unresolved.get("item_index") or 0) != item_index
    ]
    if await edit_product_clarification(callback, state, parsed, products):
        await callback.answer()
        return
    shops = await all_shops(session)
    if parsed.get("needs_shop_clarification"):
        await state.set_data({"parsed": parsed})
        await state.set_state(None)
        await callback.message.edit_text(
            "Which shop is this order for?",
            reply_markup=draft_shop_choice_keyboard(prioritized_shop_choices(shops, parsed.get("shop_candidates"))),
        )
        await callback.answer()
        return
    shop_name = sanitize_shop_name((parsed.get("shop_name") or "").upper().strip())
    shop_name, parsed["address"] = sanitize_shop_input(shop_name, parsed.get("address"), parsed.get("phone_number"))
    shop_name = sanitize_shop_name(shop_name)
    if not shop_name:
        await state.set_data({"parsed": parsed})
        await state.set_state(None)
        await callback.message.edit_text(
            draft_order_card_text(parsed),
            reply_markup=draft_order_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    parsed["shop_name"] = shop_name
    matched_shop = match_existing_shop_name(shop_name, shops)
    if not matched_shop:
        parsed["shop_name"] = None
        await state.set_data({"parsed": parsed})
        await state.set_state(None)
        await callback.message.edit_text(
            draft_order_card_text(parsed),
            reply_markup=draft_order_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    shop = matched_shop
    parsed["shop_name"] = matched_shop.name
    order = await create_order_from_parsed(session, parsed, shop, callback.from_user.id)
    await state.clear()
    await callback.message.edit_text(order_card_text(order), reply_markup=order_card_keyboard(order), parse_mode="HTML")
    await send_new_order_notification(bot, session, order.id, callback.from_user)
    await callback.answer()


@router.callback_query(OrderFlow.selecting_product, F.data.startswith("skip_product:"))
async def skip_product(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    item_index = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    parsed = data.get("parsed")
    if not parsed:
        await callback.answer("Order draft expired. Send the order again.", show_alert=True)
        return
    items = parsed.get("items", [])
    if item_index < 0 or item_index >= len(items):
        await callback.answer("Order item expired. Send the order again.", show_alert=True)
        return
    del items[item_index]
    remaining_unresolved = [
        unresolved
        for unresolved in parsed.get("unresolved_products", [])
        if int(unresolved.get("item_index") or 0) != item_index
    ]
    for unresolved in remaining_unresolved:
        current_index = int(unresolved.get("item_index") or 0)
        if current_index > item_index:
            unresolved["item_index"] = current_index - 1
    parsed["unresolved_products"] = remaining_unresolved
    products = await active_catalog(session)
    if await edit_product_clarification(callback, state, parsed, products):
        await callback.answer()
        return
    await state.update_data(parsed=parsed)
    await state.set_state(None)
    await callback.message.edit_text(
        draft_order_card_text(parsed),
        reply_markup=draft_order_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "draft:set_shop")
async def draft_set_shop(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    if not data.get("parsed"):
        await callback.answer("Order draft expired. Send the order again.", show_alert=True)
        return
    shops = await all_shops(session)
    await callback.message.edit_text(
        "Choose a shop for this order.",
        reply_markup=draft_shop_choice_keyboard(shops),
    )
    await callback.answer()


@router.callback_query(F.data == "draft:back")
async def draft_back(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    parsed = data.get("parsed")
    if not parsed:
        await callback.answer("Order draft expired. Send the order again.", show_alert=True)
        return
    await state.set_state(None)
    await callback.message.edit_text(
        draft_order_card_text(parsed),
        reply_markup=draft_order_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("draft:shop:"))
async def draft_choose_existing_shop(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    data = await state.get_data()
    parsed = data.get("parsed")
    if not parsed:
        await callback.answer("Order draft expired. Send the order again.", show_alert=True)
        return
    shop_id = int(callback.data.rsplit(":", 1)[1])
    shop = await session.get(Shop, shop_id)
    if shop is None:
        await callback.answer("Shop not found.", show_alert=True)
        return
    parsed["shop_name"] = shop.name
    order = await create_order_from_parsed(session, parsed, shop, callback.from_user.id)
    await state.clear()
    await callback.message.edit_text(order_card_text(order), reply_markup=order_card_keyboard(order), parse_mode="HTML")
    await send_new_order_notification(bot, session, order.id, callback.from_user)
    await callback.answer()


@router.callback_query(F.data == "draft:add_shop")
async def draft_add_new_shop(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("parsed"):
        await callback.answer("Order draft expired. Send the order again.", show_alert=True)
        return
    await state.set_state(OrderFlow.setting_shop)
    await callback.message.edit_text("Type the shop name for this order.")
    await callback.answer()


@router.message(OrderFlow.setting_shop, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def set_draft_shop(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    data = await state.get_data()
    parsed = data.get("parsed")
    if not parsed:
        await respond_to_message(message, "Order draft expired. Send the order again.")
        await state.clear()
        return
    shop_name = sanitize_shop_name((message.text or "").upper().strip())
    shop_name, parsed["address"] = sanitize_shop_input(shop_name, parsed.get("address"), parsed.get("phone_number"))
    shop_name = sanitize_shop_name(shop_name)
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
    await send_new_order_notification(bot, session, order.id, message.from_user)


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
    _, clean_address = sanitize_shop_input(order.shop.name, message.text)
    order.shop.address = clean_address
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


@router.callback_query(F.data.startswith("pay:"))
async def edit_payment(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("Choose payment method:", reply_markup=payment_keyboard(order_id))
    await callback.answer()


@router.callback_query(F.data.startswith("pay_method:"))
async def choose_payment_method_first(callback: CallbackQuery) -> None:
    _, raw_order_id, method = callback.data.split(":")
    await callback.message.edit_text(
        "Choose payment amount:",
        reply_markup=payment_amount_keyboard(int(raw_order_id), method),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_apply:"))
async def apply_payment_amount(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    _, mode, raw_order_id, method = callback.data.split(":")
    order_id = int(raw_order_id)
    if mode == "full":
        await set_order_payment_status_by_id(session, order_id, method)
        updated_order = await commit_payment_and_reload_order(session, order_id)
        await callback.message.edit_text(
            order_card_text(updated_order),
            reply_markup=order_card_keyboard(updated_order),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    await state.update_data(order_id=order_id, method=method)
    await state.set_state(OrderFlow.entering_split_amount)
    await callback.message.edit_text("Type payment amount.")
    await callback.answer()


@router.message(OrderFlow.entering_split_amount, F.text, F.chat.type.in_(ORDER_CHAT_TYPES))
async def enter_split_amount(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        amount = Decimal(message.text.replace(",", "").strip())
        if amount <= 0:
            raise InvalidOperation
    except Exception:
        await respond_to_message(message, "Enter a positive numeric amount.")
        return
    data = await state.get_data()
    order_id = int(data["order_id"])
    method = data.get("method")
    if not method:
        await respond_to_message(message, "Payment method expired. Open Edit Payments again.")
        await state.clear()
        return
    if amount <= 0:
        await state.clear()
        await respond_to_message(message, "Payment amount was not applied.")
        return
    await add_payment_to_order(session, order_id, method, amount)
    updated_order = await commit_payment_and_reload_order(session, order_id)
    await respond_to_message(message, order_card_text(updated_order), reply_markup=order_card_keyboard(updated_order), parse_mode="HTML")
    await state.clear()


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
        counts = await dashboard_status_counts(session)
        if not orders:
            await callback.message.edit_text("No dashboard orders found.")
        else:
            await callback.message.edit_text(
                dashboard_summary_text(orders, page=0, counts=counts),
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
        counts = await dashboard_status_counts(session)
        if not orders:
            await callback.message.edit_text("No dashboard orders found.")
        else:
            await callback.message.edit_text(
                dashboard_summary_text(orders, page=0, counts=counts),
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
