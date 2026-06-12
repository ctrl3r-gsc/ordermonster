import calendar
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


def aggregate_followup_rows(rows, mode: str) -> dict:
    shops = []
    for row in rows:
        days_since = None if row.days_since_last_order is None else int(row.days_since_last_order)
        unpaid_amount = decimal_money(row.unpaid_amount)
        if unpaid_amount > 0:
            priority = 0
            action = "Payment follow-up first"
        elif days_since is None:
            priority = 1
            action = "Introduce catalog"
        elif days_since >= 14:
            priority = 2
            action = "Send reorder message"
        else:
            priority = 3
            action = "Check reorder timing"
        shops.append(
            {
                "shop_id": row.shop_id,
                "shop_name": row.shop_name,
                "last_order_id": row.last_order_id,
                "last_order_at": row.last_order_at,
                "days_since_last_order": days_since,
                "last_order_amount": decimal_money(row.last_order_amount),
                "last_products": row.last_products or "",
                "unpaid_amount": unpaid_amount,
                "suggested_action": action,
                "priority": priority,
            }
        )
    shops.sort(
        key=lambda item: (
            item["priority"],
            -float(item["unpaid_amount"]),
            -(item["days_since_last_order"] or 9999),
            item["shop_name"],
        )
    )
    return {"mode": mode, "shops": shops}


def aggregate_product_performance_rows(rows, mode: str) -> dict:
    products = []
    for row in rows:
        products.append(
            {
                "product_id": row.product_id,
                "product_name": row.product_name,
                "quantity_sold": int(row.quantity_sold or 0),
                "revenue": decimal_money(row.revenue),
                "paid_orders": int(row.paid_orders or 0),
                "last_sold_at": row.last_sold_at,
            }
        )
    return {"mode": mode, "products": products}


def days_in_month(value: datetime) -> int:
    return calendar.monthrange(value.year, value.month)[1]


def aggregate_forecast(rows, mode: str, start_at: datetime, end_at: datetime, horizon_days: int | None = None) -> dict:
    elapsed_seconds = max((end_at - start_at).total_seconds(), 1)
    days_observed = max(elapsed_seconds / 86400, 1 / 24)
    if mode == "month":
        total_days = days_in_month(end_at.astimezone(BANGKOK_TZ))
        days_passed = max(end_at.astimezone(BANGKOK_TZ).day, 1)
        multiplier = Decimal(str(total_days)) / Decimal(str(days_passed))
        label = "This month"
    else:
        total_days = int(horizon_days or round(days_observed) or 7)
        days_passed = Decimal(str(round(days_observed, 2)))
        multiplier = Decimal(str(total_days)) / Decimal(str(days_observed))
        label = {
            "next7": "Next 7 days",
            "last7": "Based on last 7 days",
            "last30": "Based on last 30 days",
        }.get(mode, mode)

    revenue_so_far = Decimal("0.00")
    paid_order_ids: set[int] = set()
    products_sold = 0
    products = []
    for row in rows:
        quantity = int(row.quantity_sold or 0)
        revenue = decimal_money(row.revenue)
        order_count = int(row.paid_orders or 0)
        revenue_so_far += revenue
        products_sold += quantity
        if row.order_ids:
            paid_order_ids.update(int(order_id) for order_id in row.order_ids if order_id is not None)
        products.append(
            {
                "product_id": row.product_id,
                "product_name": row.product_name,
                "projected_quantity": int((Decimal(quantity) * multiplier).quantize(Decimal("1"))),
                "quantity_sold": quantity,
                "paid_orders": order_count,
            }
        )

    projected_revenue = (revenue_so_far * multiplier).quantize(Decimal("0.01"))
    projected_orders = int((Decimal(len(paid_order_ids)) * multiplier).quantize(Decimal("1")))
    projected_quantity = int((Decimal(products_sold) * multiplier).quantize(Decimal("1")))
    daily_average = (revenue_so_far / Decimal(str(days_observed))).quantize(Decimal("0.01"))
    products.sort(key=lambda item: item["projected_quantity"], reverse=True)

    return {
        "mode": mode,
        "label": label,
        "revenue_so_far": revenue_so_far.quantize(Decimal("0.01")),
        "paid_orders": len(paid_order_ids),
        "products_sold": products_sold,
        "days_passed": days_passed,
        "total_days": total_days,
        "daily_average": daily_average,
        "projected_revenue": projected_revenue,
        "projected_paid_orders": projected_orders,
        "projected_products_sold": projected_quantity,
        "products": products,
        "limited_data": len(paid_order_ids) < 3 or revenue_so_far <= 0,
    }


def aggregate_shop_analytics(sales_rows, top_product_rows, last_product_rows, shop, period: str) -> dict:
    revenue = Decimal("0.00")
    paid_orders: set[int] = set()
    quantity_sold = 0
    last_order_at = None
    unpaid_amount = Decimal("0.00")
    for row in sales_rows:
        revenue += decimal_money(row.revenue)
        quantity_sold += int(row.quantity_sold or 0)
        if row.order_ids:
            paid_orders.update(int(order_id) for order_id in row.order_ids if order_id is not None)
        if row.last_order_at and (last_order_at is None or row.last_order_at > last_order_at):
            last_order_at = row.last_order_at
        unpaid_amount = decimal_money(row.unpaid_amount)

    paid_order_count = len(paid_orders)
    average_order = (revenue / paid_order_count).quantize(Decimal("0.01")) if paid_order_count else Decimal("0.00")
    top_products = [
        {
            "product_name": row.product_name,
            "quantity_sold": int(row.quantity_sold or 0),
            "revenue": decimal_money(row.revenue),
        }
        for row in top_product_rows
    ]
    last_products = [
        {
            "product_name": row.product_name,
            "quantity": int(row.quantity or 0),
        }
        for row in last_product_rows
    ]
    if unpaid_amount > 0:
        action = "Payment follow-up first"
    elif last_products:
        action = "Offer reorder based on last purchased products"
    else:
        action = "Check in and share current catalog"
    return {
        "shop_id": shop.id,
        "shop_name": shop.name,
        "period": period,
        "paid_revenue": revenue.quantize(Decimal("0.01")),
        "paid_orders": paid_order_count,
        "average_order": average_order,
        "quantity_sold": quantity_sold,
        "last_order_at": last_order_at,
        "unpaid_amount": unpaid_amount,
        "top_products": top_products,
        "last_products": last_products,
        "suggested_action": action,
    }
