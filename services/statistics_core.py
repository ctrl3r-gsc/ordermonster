from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


try:
    BANGKOK_TZ = ZoneInfo("Asia/Bangkok")
except ZoneInfoNotFoundError:
    BANGKOK_TZ = timezone(timedelta(hours=7), "Asia/Bangkok")
UTC_TZ = timezone.utc
SUPPORTED_PERIODS = {"today", "week", "month", "all"}


def period_bounds(period: str, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    if period not in SUPPORTED_PERIODS:
        raise ValueError(f"Unsupported statistics period: {period}")
    if period == "all":
        return None, None

    current = now or datetime.now(BANGKOK_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BANGKOK_TZ)
    local_now = current.astimezone(BANGKOK_TZ)

    if period == "today":
        start_date = local_now.date()
    elif period == "week":
        # Current calendar week, Monday 00:00 in Bangkok time.
        start_date = local_now.date() - timedelta(days=local_now.weekday())
    else:
        start_date = local_now.date().replace(day=1)

    local_start = datetime.combine(start_date, time.min, tzinfo=BANGKOK_TZ)
    return local_start.astimezone(UTC_TZ), local_now.astimezone(UTC_TZ)


def decimal_money(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def aggregate_stats_rows(rows, total_paid_orders: int, period: str) -> dict:
    products = []
    total_revenue = Decimal("0.00")
    total_quantity = 0

    for row in rows:
        quantity_sold = int(row.quantity_sold or 0)
        gift_quantity = int(row.gift_quantity or 0)
        revenue = decimal_money(row.revenue)
        if quantity_sold <= 0:
            continue
        total_quantity += quantity_sold
        total_revenue += revenue
        products.append(
            {
                "product_id": row.product_id,
                "product_name": row.product_name,
                "quantity_sold": quantity_sold,
                "revenue": revenue,
                "gift_quantity": gift_quantity,
            }
        )

    return {
        "period": period,
        "total_revenue": total_revenue.quantize(Decimal("0.01")),
        "total_paid_orders": int(total_paid_orders or 0),
        "total_quantity": total_quantity,
        "products": products,
    }


def aggregate_debt_rows(rows, mode: str) -> dict:
    orders = []
    total_debt = Decimal("0.00")

    for row in rows:
        amount = decimal_money(row.debt_amount)
        total_debt += amount
        orders.append(
            {
                "order_id": row.order_id,
                "display_number": row.display_number or row.order_id,
                "shop_name": row.shop_name,
                "debt_amount": amount,
                "delivery_status": row.delivery_status,
                "created_at": row.created_at,
                "age_days": int(row.age_days or 0),
            }
        )

    return {
        "mode": mode,
        "total_debt": total_debt.quantize(Decimal("0.01")),
        "order_count": len(orders),
        "orders": orders,
    }


def aggregate_debt_shop_rows(rows, mode: str = "shops") -> dict:
    shops = []
    total_debt = Decimal("0.00")
    total_orders = 0

    for row in rows:
        amount = decimal_money(row.debt_amount)
        order_count = int(row.order_count or 0)
        total_debt += amount
        total_orders += order_count
        shops.append(
            {
                "shop_id": row.shop_id,
                "shop_name": row.shop_name,
                "debt_amount": amount,
                "order_count": order_count,
            }
        )

    return {
        "mode": mode,
        "total_debt": total_debt.quantize(Decimal("0.01")),
        "order_count": total_orders,
        "shops": shops,
    }


def aggregate_shop_sales_rows(rows, period: str) -> dict:
    shops = []
    total_revenue = Decimal("0.00")
    total_paid_orders = 0

    for row in rows:
        paid_orders = int(row.paid_orders or 0)
        revenue = decimal_money(row.revenue)
        quantity_sold = int(row.quantity_sold or 0)
        unpaid_amount = decimal_money(row.unpaid_amount)
        average_order = (revenue / paid_orders).quantize(Decimal("0.01")) if paid_orders else Decimal("0.00")
        if paid_orders <= 0 and revenue <= 0:
            continue
        total_revenue += revenue
        total_paid_orders += paid_orders
        shops.append(
            {
                "shop_id": row.shop_id,
                "shop_name": row.shop_name,
                "revenue": revenue,
                "paid_orders": paid_orders,
                "average_order": average_order,
                "quantity_sold": quantity_sold,
                "last_order_at": row.last_order_at,
                "unpaid_amount": unpaid_amount,
            }
        )

    return {
        "period": period,
        "total_revenue": total_revenue.quantize(Decimal("0.01")),
        "total_paid_orders": total_paid_orders,
        "active_shops": len(shops),
        "shops": shops,
    }
