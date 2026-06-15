import logging
from datetime import UTC, datetime
from decimal import Decimal
from html import escape

from aiogram import Bot
from aiogram.types import User
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import BotSetting, Order
from services.orders import display_order_number, get_order, item_subtotal, item_unit_price


logger = logging.getLogger(__name__)
ORDER_NOTIFICATIONS_CHAT_ID_KEY = "order_notifications_chat_id"


async def get_notification_chat_id(session: AsyncSession) -> int | None:
    setting = await session.get(BotSetting, ORDER_NOTIFICATIONS_CHAT_ID_KEY)
    if not setting:
        return None
    try:
        return int(setting.value)
    except (TypeError, ValueError):
        logger.warning("Invalid order notification chat_id stored: %r", setting.value)
        return None


async def set_notification_chat_id(session: AsyncSession, chat_id: int) -> None:
    setting = await session.get(BotSetting, ORDER_NOTIFICATIONS_CHAT_ID_KEY)
    if setting:
        setting.value = str(chat_id)
        setting.updated_at = datetime.now(UTC)
    else:
        session.add(BotSetting(key=ORDER_NOTIFICATIONS_CHAT_ID_KEY, value=str(chat_id)))
    await session.flush()


async def disable_order_notifications(session: AsyncSession) -> None:
    await session.execute(delete(BotSetting).where(BotSetting.key == ORDER_NOTIFICATIONS_CHAT_ID_KEY))
    await session.flush()


def _money(value: Decimal | int | str | None) -> str:
    amount = Decimal(str(value or 0))
    return f"{amount:,.2f}".rstrip("0").rstrip(".")


def _status_label(value) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "unknown").replace("_", " ").title()


def _created_by_label(created_by_user: User | None, fallback_user_id: int | None = None) -> str:
    if created_by_user:
        if created_by_user.username:
            return f"@{created_by_user.username}"
        name = " ".join(part for part in (created_by_user.first_name, created_by_user.last_name) if part)
        return name or str(created_by_user.id)
    return str(fallback_user_id or "unknown")


def format_new_order_notification(order: Order, created_by_user: User | None = None) -> str:
    lines = [
        f"🆕 <b>New order #{display_order_number(order)}</b>",
        "",
        f"🏪 Shop: <b>{escape(order.shop.name)}</b>",
        f"💰 Total: <b>{_money(order.total_amount)} THB</b>",
        "📦 Items:",
    ]
    visible_items = list(order.items[:20])
    for item in visible_items:
        product_name = escape(" ".join(str(item.product.name).split()))
        unit_price = item_unit_price(item)
        line_total = item_subtotal(item)
        lines.append(
            f"• {product_name} — {item.quantity} pcs × {_money(unit_price)} = {_money(line_total)} THB"
        )
    if len(order.items) > len(visible_items):
        lines.append(f"• ... and {len(order.items) - len(visible_items)} more item(s)")
    lines.extend(
        [
            "",
            f"💳 Payment: {_status_label(order.payment_status)}",
            f"🚚 Delivery: {_status_label(order.delivery_status)}",
            "",
            f"Created by: {escape(_created_by_label(created_by_user, order.user_id))}",
        ]
    )
    return "\n".join(lines)


async def send_notification_test(bot: Bot, session: AsyncSession) -> bool:
    chat_id = await get_notification_chat_id(session)
    if chat_id is None:
        return False
    await bot.send_message(
        chat_id,
        "🔔 <b>Test notification</b>\n\nOrderMonster notifications are working.",
        parse_mode="HTML",
    )
    return True


async def send_new_order_notification(
    bot: Bot,
    session: AsyncSession,
    order_id: int,
    created_by_user: User | None = None,
) -> bool:
    chat_id = await get_notification_chat_id(session)
    if chat_id is None:
        return False

    order = await get_order(session, order_id)
    if order.notification_sent_at is not None:
        return False

    try:
        await bot.send_message(chat_id, format_new_order_notification(order, created_by_user), parse_mode="HTML")
    except Exception:
        logger.exception("Failed to send new order notification for order_id=%s", order_id)
        return False

    try:
        order.notification_sent_at = datetime.now(UTC)
        await session.flush()
        await session.commit()
    except Exception:
        logger.exception("Failed to mark order notification as sent for order_id=%s", order_id)
        await session.rollback()
        return False
    return True
