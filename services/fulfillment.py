from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select, text
from db.models import AssemblyShipmentCommand, DeliveryStatus, Order


class ShipmentCommand(BaseModel):
    request_id: UUID
    order_id: int = Field(gt=0)
    action: Literal["ship", "tracking", "delivered", "reopen"]
    actor_id: int = Field(gt=0)
    actor_name: str = Field(min_length=1, max_length=255)
    occurred_at: AwareDatetime
    tracking_number: str | None = Field(default=None, max_length=255)
    expected_tracking_number: str | None = Field(default=None, max_length=255)


class ShipmentConflict(ValueError):
    pass


def fulfillment(order):
    return {"id": str(order.id), "delivery_status": order.delivery_status.value,
            "tracking_number": order.tracking_number,
            "shipped_by_telegram_id": order.shipped_by_telegram_id,
            "shipped_by_name": order.shipped_by_name,
            "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
            "shipment_source": order.shipment_source}


def record_shipment(order, actor_id, actor_name, tracking_number=None, source="ordermonster", at=None):
    if order.delivery_status == DeliveryStatus.pending_shipment:
        order.delivery_status = DeliveryStatus.shipped
        order.shipped_by_telegram_id = actor_id
        order.shipped_by_name = actor_name[:255]
        order.shipped_at = at or datetime.now(timezone.utc)
        order.shipment_source = source
    if tracking_number and tracking_number.strip():
        order.tracking_number = tracking_number.strip()


async def locked_order(session, order_id):
    return await session.scalar(select(Order).where(Order.id == order_id).with_for_update()
                                .execution_options(populate_existing=True))


async def apply_command(session, command: ShipmentCommand):
    fingerprint = hashlib.sha256(json.dumps(command.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    key = str(command.request_id)
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
    receipt = await session.get(AssemblyShipmentCommand, key)
    if receipt:
        if receipt.payload_hash != fingerprint:
            raise ShipmentConflict("Повторный запрос содержит другие данные")
        return receipt.result
    order = await locked_order(session, command.order_id)
    if order is None:
        raise LookupError("Заказ не найден")
    track = (command.tracking_number or "").strip()
    if command.action == "tracking":
        if not track:
            raise ShipmentConflict("Укажите трек-номер")
        if order.delivery_status == DeliveryStatus.pending_shipment:
            raise ShipmentConflict("Заказ ещё не отправлен")
        if (order.tracking_number or "") != (command.expected_tracking_number or ""):
            raise ShipmentConflict("Трек изменён в OrderMonster. Обновите заказ и повторите действие")
        order.tracking_number = track
    elif command.action == "delivered":
        if order.delivery_status != DeliveryStatus.shipped:
            raise ShipmentConflict("Заказ ещё не отправлен или уже доставлен")
        order.delivery_status = DeliveryStatus.delivered
    elif command.action == "reopen":
        if order.delivery_status != DeliveryStatus.shipped:
            raise ShipmentConflict("Вернуть в работу можно только отправленный заказ")
        order.delivery_status = DeliveryStatus.pending_shipment
        order.tracking_number = None
        order.shipped_by_telegram_id = None
        order.shipped_by_name = None
        order.shipped_at = None
        order.shipment_source = None
    elif order.delivery_status == DeliveryStatus.pending_shipment:
        if track and (order.tracking_number or "") != (command.expected_tracking_number or ""):
            raise ShipmentConflict("Трек изменён в OrderMonster. Обновите заказ и повторите действие")
        record_shipment(order, command.actor_id, command.actor_name, track, "assembly", command.occurred_at)
    result = fulfillment(order)
    result["command_action"] = command.action
    if command.action == "ship" and track and track != (order.tracking_number or ""):
        result["note"] = "Заказ уже отправлен в OrderMonster; трек сохранён. Изменить его можно отдельно."
    session.add(AssemblyShipmentCommand(request_id=key, payload_hash=fingerprint, result=result))
    await session.flush()
    return result
