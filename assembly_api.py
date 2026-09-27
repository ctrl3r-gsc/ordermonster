"""Read-only OrderMonster export. Runs separately from bot polling."""
import hmac
import os
from datetime import datetime, timezone

from aiohttp import web
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from db.models import DeliveryStatus, Order, OrderItem
from db.session import SessionLocal

MAX_ORDERS = 10000


async def orders(request: web.Request) -> web.Response:
    token = request.app["export_token"]
    if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
        raise web.HTTPUnauthorized()
    async with SessionLocal() as session:
        # One consistent, read-only snapshot across orders, shops and positions.
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        result = await session.scalars(
            select(Order).where(Order.delivery_status == DeliveryStatus.pending_shipment).options(selectinload(Order.shop), selectinload(Order.items).selectinload(OrderItem.product))
            .order_by(Order.id).limit(MAX_ORDERS + 1)
        )
        records = result.all()
        if len(records) > MAX_ORDERS:
            raise web.HTTPServiceUnavailable(text="Snapshot limit exceeded; pagination required")
        payload = [{
            "id": str(order.id),
            "display_number": str(order.display_number or order.id),
            "shop_name": order.shop.name,
            "delivery_status": order.delivery_status.value,
            "payment_status": order.payment_status.value,
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat(),
            "items": [{"product_id": str(item.product_id), "product_name": item.product.name,
                       "quantity": str(item.quantity)} for item in order.items],
        } for order in records]
    return web.json_response({"orders": payload, "generated_at": datetime.now(timezone.utc).isoformat()},
                             headers={"Cache-Control": "no-store"})


def create_app(token: str | None = None) -> web.Application:
    token = token if token is not None else os.environ.get("ASSEMBLY_EXPORT_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("ASSEMBLY_EXPORT_TOKEN must contain at least 32 characters")
    app = web.Application()
    app["export_token"] = token
    app.router.add_get("/v1/orders", orders)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8080, access_log=None)
