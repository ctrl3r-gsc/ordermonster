from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryStatus, Order, OrderItem, OrderPayment, PaymentStatus, Product, Shop
from services.statistics_core import (
    BANGKOK_TZ,
    aggregate_followup_rows,
    aggregate_forecast,
    aggregate_product_performance_rows,
    aggregate_shop_analytics,
    aggregate_debt_rows,
    aggregate_debt_shop_rows,
    aggregate_shop_sales_rows,
    aggregate_stats_rows,
    period_bounds,
)


MAX_ANALYTICS_ROWS = 20


def _apply_period_filter(stmt, start_at: datetime | None, end_at: datetime | None):
    if start_at is not None:
        stmt = stmt.where(Order.created_at >= start_at)
    if end_at is not None:
        stmt = stmt.where(Order.created_at <= end_at)
    return stmt


async def get_product_sales_stats(session: AsyncSession, period: str) -> dict:
    start_at, end_at = period_bounds(period)

    paid_orders_stmt = select(func.count(distinct(Order.id))).where(Order.payment_status == PaymentStatus.paid)
    paid_orders_stmt = _apply_period_filter(paid_orders_stmt, start_at, end_at)
    total_paid_orders = await session.scalar(paid_orders_stmt)

    non_gift_quantity = case((OrderItem.is_gift.is_not(True), OrderItem.quantity), else_=0)
    non_gift_revenue = case(
        (OrderItem.is_gift.is_not(True), OrderItem.quantity * OrderItem.price_per_unit),
        else_=0,
    )
    gift_quantity = case((OrderItem.is_gift.is_(True), OrderItem.quantity), else_=0)

    rows_stmt = (
        select(
            Product.id.label("product_id"),
            Product.name.label("product_name"),
            func.coalesce(func.sum(non_gift_quantity), 0).label("quantity_sold"),
            func.coalesce(func.sum(non_gift_revenue), 0).label("revenue"),
            func.coalesce(func.sum(gift_quantity), 0).label("gift_quantity"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.payment_status == PaymentStatus.paid)
        .group_by(Product.id, Product.name)
        .order_by(func.coalesce(func.sum(non_gift_revenue), 0).desc(), Product.name.asc())
    )
    rows_stmt = _apply_period_filter(rows_stmt, start_at, end_at)
    rows = (await session.execute(rows_stmt)).all()

    stats = aggregate_stats_rows(rows, int(total_paid_orders or 0), period)
    stats["limited"] = len(stats["products"]) > MAX_ANALYTICS_ROWS
    stats["products"] = stats["products"][:MAX_ANALYTICS_ROWS]
    return stats


def _payment_totals_subquery():
    return (
        select(
            OrderPayment.order_id.label("order_id"),
            func.coalesce(func.sum(OrderPayment.amount), 0).label("paid_amount"),
        )
        .group_by(OrderPayment.order_id)
        .subquery()
    )


def _debt_amount_expr(payment_totals):
    return func.coalesce(Order.total_amount, 0) - func.coalesce(payment_totals.c.paid_amount, 0)


def _age_days_expr():
    return func.floor(func.extract("epoch", func.now() - Order.created_at) / 86400)


def _non_gift_quantity_expr():
    return case((OrderItem.is_gift.is_not(True), OrderItem.quantity), else_=0)


def _non_gift_revenue_expr():
    return case(
        (OrderItem.is_gift.is_not(True), OrderItem.quantity * OrderItem.price_per_unit),
        else_=0,
    )


async def _order_product_summary(session: AsyncSession, order_id: int | None, limit: int = 4) -> str:
    if not order_id:
        return ""
    rows = (
        await session.execute(
            select(Product.name.label("product_name"), OrderItem.quantity.label("quantity"))
            .join(OrderItem, OrderItem.product_id == Product.id)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.id.asc())
            .limit(limit)
        )
    ).all()
    return ", ".join(f"{row.product_name} x{int(row.quantity or 0)}" for row in rows)


async def get_debt_stats(session: AsyncSession, mode: str = "delivered") -> dict:
    payment_totals = _payment_totals_subquery()
    debt_amount = _debt_amount_expr(payment_totals)

    base_filters = [Order.payment_status != PaymentStatus.paid]
    if mode in {"delivered", "shops", "old7"}:
        base_filters.append(Order.delivery_status == DeliveryStatus.delivered)
    if mode == "old7":
        base_filters.append(Order.created_at <= datetime.now(BANGKOK_TZ) - timedelta(days=7))

    if mode == "shops":
        rows_stmt = (
            select(
                Shop.id.label("shop_id"),
                Shop.name.label("shop_name"),
                func.coalesce(func.sum(debt_amount), 0).label("debt_amount"),
                func.count(Order.id).label("order_count"),
            )
            .join(Shop, Shop.id == Order.shop_id)
            .outerjoin(payment_totals, payment_totals.c.order_id == Order.id)
            .where(*base_filters)
            .group_by(Shop.id, Shop.name)
            .order_by(func.coalesce(func.sum(debt_amount), 0).desc(), Shop.name.asc())
        )
        rows = (await session.execute(rows_stmt)).all()
        stats = aggregate_debt_shop_rows(rows, mode)
        stats["limited"] = len(stats["shops"]) > MAX_ANALYTICS_ROWS
        stats["shops"] = stats["shops"][:MAX_ANALYTICS_ROWS]
        return stats

    rows_stmt = (
        select(
            Order.id.label("order_id"),
            Order.display_number.label("display_number"),
            Shop.name.label("shop_name"),
            debt_amount.label("debt_amount"),
            Order.delivery_status.label("delivery_status"),
            Order.created_at.label("created_at"),
            _age_days_expr().label("age_days"),
        )
        .join(Shop, Shop.id == Order.shop_id)
        .outerjoin(payment_totals, payment_totals.c.order_id == Order.id)
        .where(*base_filters)
        .order_by(debt_amount.desc(), Order.created_at.asc())
    )
    rows = (await session.execute(rows_stmt)).all()
    stats = aggregate_debt_rows(rows, mode)
    stats["limited"] = len(stats["orders"]) > MAX_ANALYTICS_ROWS
    stats["orders"] = stats["orders"][:MAX_ANALYTICS_ROWS]
    return stats


async def get_shop_sales_stats(session: AsyncSession, period: str) -> dict:
    start_at, end_at = period_bounds(period)
    payment_totals = _payment_totals_subquery()

    non_gift_quantity = case((OrderItem.is_gift.is_not(True), OrderItem.quantity), else_=0)
    non_gift_revenue = case(
        (OrderItem.is_gift.is_not(True), OrderItem.quantity * OrderItem.price_per_unit),
        else_=0,
    )

    paid_filters = [Order.payment_status == PaymentStatus.paid]
    if start_at is not None:
        paid_filters.append(Order.created_at >= start_at)
    if end_at is not None:
        paid_filters.append(Order.created_at <= end_at)

    paid_sales = (
        select(
            Shop.id.label("shop_id"),
            Shop.name.label("shop_name"),
            func.coalesce(func.sum(non_gift_revenue), 0).label("revenue"),
            func.count(distinct(Order.id)).label("paid_orders"),
            func.coalesce(func.sum(non_gift_quantity), 0).label("quantity_sold"),
            func.max(Order.created_at).label("last_order_at"),
        )
        .select_from(Shop)
        .join(Order, Order.shop_id == Shop.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(*paid_filters)
        .group_by(Shop.id, Shop.name)
        .subquery()
    )

    unpaid_filters = [Order.payment_status != PaymentStatus.paid]
    if start_at is not None:
        unpaid_filters.append(Order.created_at >= start_at)
    if end_at is not None:
        unpaid_filters.append(Order.created_at <= end_at)

    unpaid_by_shop = (
        select(
            Order.shop_id.label("shop_id"),
            func.coalesce(func.sum(_debt_amount_expr(payment_totals)), 0).label("unpaid_amount"),
        )
        .outerjoin(payment_totals, payment_totals.c.order_id == Order.id)
        .where(*unpaid_filters)
        .group_by(Order.shop_id)
        .subquery()
    )

    rows_stmt = (
        select(
            paid_sales.c.shop_id,
            paid_sales.c.shop_name,
            paid_sales.c.revenue,
            paid_sales.c.paid_orders,
            paid_sales.c.quantity_sold,
            paid_sales.c.last_order_at,
            func.coalesce(unpaid_by_shop.c.unpaid_amount, 0).label("unpaid_amount"),
        )
        .outerjoin(unpaid_by_shop, unpaid_by_shop.c.shop_id == paid_sales.c.shop_id)
        .order_by(paid_sales.c.revenue.desc(), paid_sales.c.shop_name.asc())
    )
    rows = (await session.execute(rows_stmt)).all()
    stats = aggregate_shop_sales_rows(rows, period)
    stats["limited"] = len(stats["shops"]) > MAX_ANALYTICS_ROWS
    stats["shops"] = stats["shops"][:MAX_ANALYTICS_ROWS]
    return stats


async def get_shop_analytics(session: AsyncSession, shop_id: int, period: str = "month") -> dict:
    shop = await session.get(Shop, shop_id)
    if shop is None:
        raise ValueError(f"Shop {shop_id} not found")

    start_at, end_at = period_bounds(period)
    payment_totals = _payment_totals_subquery()
    quantity_expr = _non_gift_quantity_expr()
    revenue_expr = _non_gift_revenue_expr()

    paid_filters = [
        Order.shop_id == shop_id,
        Order.payment_status == PaymentStatus.paid,
        OrderItem.is_gift.is_not(True),
    ]
    if start_at is not None:
        paid_filters.append(Order.created_at >= start_at)
    if end_at is not None:
        paid_filters.append(Order.created_at <= end_at)

    unpaid_amount = await session.scalar(
        select(func.coalesce(func.sum(_debt_amount_expr(payment_totals)), 0))
        .select_from(Order)
        .outerjoin(payment_totals, payment_totals.c.order_id == Order.id)
        .where(Order.shop_id == shop_id, Order.payment_status != PaymentStatus.paid)
    )

    summary_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(quantity_expr), 0).label("quantity_sold"),
                func.coalesce(func.sum(revenue_expr), 0).label("revenue"),
                func.array_agg(distinct(Order.id)).label("order_ids"),
                func.max(Order.created_at).label("last_order_at"),
                func.coalesce(unpaid_amount, 0).label("unpaid_amount"),
            )
            .select_from(Order)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(*paid_filters)
        )
    ).one()

    top_product_rows = (
        await session.execute(
            select(
                Product.name.label("product_name"),
                func.coalesce(func.sum(quantity_expr), 0).label("quantity_sold"),
                func.coalesce(func.sum(revenue_expr), 0).label("revenue"),
            )
            .select_from(Order)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(*paid_filters)
            .group_by(Product.id, Product.name)
            .order_by(func.coalesce(func.sum(revenue_expr), 0).desc(), Product.name.asc())
            .limit(5)
        )
    ).all()

    last_order_id = await session.scalar(
        select(Order.id)
        .where(Order.shop_id == shop_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(1)
    )
    last_product_rows = []
    if last_order_id:
        last_product_rows = (
            await session.execute(
                select(Product.name.label("product_name"), OrderItem.quantity.label("quantity"))
                .select_from(OrderItem)
                .join(Product, Product.id == OrderItem.product_id)
                .where(OrderItem.order_id == last_order_id)
                .order_by(OrderItem.id.asc())
                .limit(5)
            )
        ).all()

    return aggregate_shop_analytics([summary_row], top_product_rows, last_product_rows, shop, period)


async def get_followup_stats(session: AsyncSession, mode: str = "attention") -> dict:
    payment_totals = _payment_totals_subquery()
    debt_amount = _debt_amount_expr(payment_totals)

    last_orders_ranked = (
        select(
            Order.id.label("order_id"),
            Order.shop_id.label("shop_id"),
            Order.created_at.label("created_at"),
            Order.total_amount.label("total_amount"),
            func.row_number()
            .over(partition_by=Order.shop_id, order_by=(Order.created_at.desc(), Order.id.desc()))
            .label("row_number"),
        )
        .subquery()
    )
    last_orders = (
        select(
            last_orders_ranked.c.order_id,
            last_orders_ranked.c.shop_id,
            last_orders_ranked.c.created_at,
            last_orders_ranked.c.total_amount,
        )
        .where(last_orders_ranked.c.row_number == 1)
        .subquery()
    )
    unpaid_by_shop = (
        select(
            Order.shop_id.label("shop_id"),
            func.coalesce(func.sum(debt_amount), 0).label("unpaid_amount"),
        )
        .outerjoin(payment_totals, payment_totals.c.order_id == Order.id)
        .where(Order.payment_status != PaymentStatus.paid, Order.delivery_status == DeliveryStatus.delivered)
        .group_by(Order.shop_id)
        .subquery()
    )
    days_since = func.floor(func.extract("epoch", func.now() - last_orders.c.created_at) / 86400)
    rows_stmt = (
        select(
            Shop.id.label("shop_id"),
            Shop.name.label("shop_name"),
            last_orders.c.order_id.label("last_order_id"),
            last_orders.c.created_at.label("last_order_at"),
            days_since.label("days_since_last_order"),
            func.coalesce(last_orders.c.total_amount, 0).label("last_order_amount"),
            func.coalesce(unpaid_by_shop.c.unpaid_amount, 0).label("unpaid_amount"),
        )
        .select_from(Shop)
        .outerjoin(last_orders, last_orders.c.shop_id == Shop.id)
        .outerjoin(unpaid_by_shop, unpaid_by_shop.c.shop_id == Shop.id)
    )
    rows = (await session.execute(rows_stmt)).all()
    enriched_rows = []
    for row in rows:
        last_products = await _order_product_summary(session, row.last_order_id)
        enriched_rows.append(
            SimpleNamespace(
                shop_id=row.shop_id,
                shop_name=row.shop_name,
                last_order_id=row.last_order_id,
                last_order_at=row.last_order_at,
                days_since_last_order=row.days_since_last_order,
                last_order_amount=row.last_order_amount,
                last_products=last_products,
                unpaid_amount=row.unpaid_amount,
            )
        )

    stats = aggregate_followup_rows(enriched_rows, mode)
    if mode == "days7":
        stats["shops"] = [
            shop for shop in stats["shops"] if shop["days_since_last_order"] is None or shop["days_since_last_order"] >= 7
        ]
    elif mode == "days14":
        stats["shops"] = [
            shop for shop in stats["shops"] if shop["days_since_last_order"] is None or shop["days_since_last_order"] >= 14
        ]
    elif mode == "days30":
        stats["shops"] = [
            shop for shop in stats["shops"] if shop["days_since_last_order"] is None or shop["days_since_last_order"] >= 30
        ]
    elif mode == "debts":
        stats["shops"] = [shop for shop in stats["shops"] if shop["unpaid_amount"] > 0]
    else:
        stats["shops"] = [
            shop
            for shop in stats["shops"]
            if shop["unpaid_amount"] > 0 or shop["days_since_last_order"] is None or shop["days_since_last_order"] >= 7
        ]
    stats["limited"] = len(stats["shops"]) > MAX_ANALYTICS_ROWS
    stats["shops"] = stats["shops"][:MAX_ANALYTICS_ROWS]
    return stats


async def get_product_performance_stats(session: AsyncSession, mode: str = "best_revenue") -> dict:
    quantity_expr = _non_gift_quantity_expr()
    revenue_expr = _non_gift_revenue_expr()
    now = datetime.now(BANGKOK_TZ)
    month_start, month_end = period_bounds("month", now)
    slow_days = {"slow7": 7, "slow14": 14, "slow30": 30}.get(mode)
    period_start = None
    period_end = None
    if slow_days:
        period_start = now - timedelta(days=slow_days)
        period_end = now
    elif mode == "no_sales_month":
        period_start = month_start
        period_end = month_end

    if mode in {"best_revenue", "best_qty"}:
        rows_stmt = (
            select(
                Product.id.label("product_id"),
                Product.name.label("product_name"),
                func.coalesce(func.sum(quantity_expr), 0).label("quantity_sold"),
                func.coalesce(func.sum(revenue_expr), 0).label("revenue"),
                func.count(distinct(Order.id)).label("paid_orders"),
                func.max(Order.created_at).label("last_sold_at"),
            )
            .join(OrderItem, OrderItem.product_id == Product.id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.payment_status == PaymentStatus.paid, OrderItem.is_gift.is_not(True))
            .group_by(Product.id, Product.name)
        )
        if mode == "best_qty":
            rows_stmt = rows_stmt.order_by(func.coalesce(func.sum(quantity_expr), 0).desc(), Product.name.asc())
        else:
            rows_stmt = rows_stmt.order_by(func.coalesce(func.sum(revenue_expr), 0).desc(), Product.name.asc())
        rows = (await session.execute(rows_stmt)).all()
        stats = aggregate_product_performance_rows(rows, mode)
        stats["limited"] = len(stats["products"]) > MAX_ANALYTICS_ROWS
        stats["products"] = stats["products"][:MAX_ANALYTICS_ROWS]
        return stats

    sales_filters = [Order.payment_status == PaymentStatus.paid, OrderItem.is_gift.is_not(True)]
    if period_start is not None:
        sales_filters.append(Order.created_at >= period_start)
    if period_end is not None:
        sales_filters.append(Order.created_at <= period_end)
    period_sales = (
        select(
            OrderItem.product_id.label("product_id"),
            func.coalesce(func.sum(quantity_expr), 0).label("quantity_sold"),
            func.coalesce(func.sum(revenue_expr), 0).label("revenue"),
            func.count(distinct(Order.id)).label("paid_orders"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(*sales_filters)
        .group_by(OrderItem.product_id)
        .subquery()
    )
    last_sales = (
        select(OrderItem.product_id.label("product_id"), func.max(Order.created_at).label("last_sold_at"))
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.payment_status == PaymentStatus.paid, OrderItem.is_gift.is_not(True))
        .group_by(OrderItem.product_id)
        .subquery()
    )
    rows_stmt = (
        select(
            Product.id.label("product_id"),
            Product.name.label("product_name"),
            func.coalesce(period_sales.c.quantity_sold, 0).label("quantity_sold"),
            func.coalesce(period_sales.c.revenue, 0).label("revenue"),
            func.coalesce(period_sales.c.paid_orders, 0).label("paid_orders"),
            last_sales.c.last_sold_at.label("last_sold_at"),
        )
        .select_from(Product)
        .outerjoin(period_sales, period_sales.c.product_id == Product.id)
        .outerjoin(last_sales, last_sales.c.product_id == Product.id)
        .where(Product.is_active.is_(True), func.coalesce(period_sales.c.quantity_sold, 0) == 0)
        .order_by(last_sales.c.last_sold_at.asc().nulls_first(), Product.name.asc())
    )
    rows = (await session.execute(rows_stmt)).all()
    stats = aggregate_product_performance_rows(rows, mode)
    stats["limited"] = len(stats["products"]) > MAX_ANALYTICS_ROWS
    stats["products"] = stats["products"][:MAX_ANALYTICS_ROWS]
    return stats


async def get_sales_forecast(session: AsyncSession, mode: str = "month") -> dict:
    now = datetime.now(BANGKOK_TZ)
    if mode == "month":
        start_at, end_at = period_bounds("month", now)
        horizon_days = None
    elif mode in {"next7", "last7"}:
        end_at = now
        start_at = now - timedelta(days=7)
        horizon_days = 7
    else:
        end_at = now
        start_at = now - timedelta(days=30)
        horizon_days = 30

    quantity_expr = _non_gift_quantity_expr()
    revenue_expr = _non_gift_revenue_expr()
    rows_stmt = (
        select(
            Product.id.label("product_id"),
            Product.name.label("product_name"),
            func.coalesce(func.sum(quantity_expr), 0).label("quantity_sold"),
            func.coalesce(func.sum(revenue_expr), 0).label("revenue"),
            func.count(distinct(Order.id)).label("paid_orders"),
            func.array_agg(distinct(Order.id)).label("order_ids"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.payment_status == PaymentStatus.paid,
            OrderItem.is_gift.is_not(True),
            Order.created_at >= start_at,
            Order.created_at <= end_at,
        )
        .group_by(Product.id, Product.name)
        .order_by(func.coalesce(func.sum(quantity_expr), 0).desc(), Product.name.asc())
    )
    rows = (await session.execute(rows_stmt)).all()
    stats = aggregate_forecast(rows, mode, start_at, end_at, horizon_days)
    stats["limited"] = len(stats["products"]) > MAX_ANALYTICS_ROWS
    stats["products"] = stats["products"][:MAX_ANALYTICS_ROWS]
    return stats
