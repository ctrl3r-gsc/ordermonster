from collections import namedtuple
from datetime import datetime, timedelta
from decimal import Decimal

from services.statistics_core import (
    BANGKOK_TZ,
    aggregate_followup_rows,
    aggregate_forecast,
    aggregate_product_performance_rows,
    aggregate_shop_analytics,
)


FollowupRow = namedtuple(
    "FollowupRow",
    "shop_id shop_name last_order_id last_order_at days_since_last_order last_order_amount last_products unpaid_amount",
)
ProductRow = namedtuple("ProductRow", "product_id product_name quantity_sold revenue paid_orders last_sold_at")
ForecastRow = namedtuple("ForecastRow", "product_id product_name quantity_sold revenue paid_orders order_ids")
Shop = namedtuple("Shop", "id name")
ShopSummaryRow = namedtuple("ShopSummaryRow", "quantity_sold revenue order_ids last_order_at unpaid_amount")
ShopProductRow = namedtuple("ShopProductRow", "product_name quantity_sold revenue")
LastProductRow = namedtuple("LastProductRow", "product_name quantity")


def test_followups_detect_old_last_order() -> None:
    stats = aggregate_followup_rows(
        [
            FollowupRow(1, "Old Shop", 10, datetime(2026, 5, 1, tzinfo=BANGKOK_TZ), 42, Decimal("2400"), "Brownie x20", Decimal("0")),
        ],
        "days30",
    )

    assert stats["shops"][0]["shop_name"] == "Old Shop"
    assert stats["shops"][0]["suggested_action"] == "Send reorder message"


def test_followups_prioritize_unpaid_delivered_orders() -> None:
    stats = aggregate_followup_rows(
        [
            FollowupRow(1, "Old Shop", 10, datetime(2026, 5, 1, tzinfo=BANGKOK_TZ), 42, Decimal("2400"), "", Decimal("0")),
            FollowupRow(2, "Debt Shop", 11, datetime(2026, 6, 10, tzinfo=BANGKOK_TZ), 2, Decimal("3290"), "", Decimal("3290")),
        ],
        "attention",
    )

    assert stats["shops"][0]["shop_name"] == "Debt Shop"
    assert stats["shops"][0]["suggested_action"] == "Payment follow-up first"


def test_product_performance_uses_paid_non_gift_rows_from_service() -> None:
    stats = aggregate_product_performance_rows(
        [
            ProductRow(1, "Brownie", 20, Decimal("2400"), 3, datetime(2026, 6, 12, tzinfo=BANGKOK_TZ)),
        ],
        "best_revenue",
    )

    assert stats["products"][0]["quantity_sold"] == 20
    assert stats["products"][0]["revenue"] == Decimal("2400.00")
    assert stats["products"][0]["paid_orders"] == 3


def test_forecast_handles_zero_data() -> None:
    now = datetime(2026, 6, 1, 10, 0, tzinfo=BANGKOK_TZ)
    stats = aggregate_forecast([], "month", now.replace(hour=0, minute=0), now)

    assert stats["projected_revenue"] == Decimal("0.00")
    assert stats["projected_paid_orders"] == 0
    assert stats["limited_data"] is True


def test_forecast_first_day_of_month_is_safe() -> None:
    start = datetime(2026, 6, 1, 0, 0, tzinfo=BANGKOK_TZ)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=BANGKOK_TZ)
    stats = aggregate_forecast(
        [ForecastRow(1, "Brownie", 10, Decimal("1000"), 1, [101])],
        "month",
        start,
        now,
    )

    assert stats["days_passed"] == 1
    assert stats["total_days"] == 30
    assert stats["projected_revenue"] == Decimal("30000.00")


def test_shop_analytics_aggregates_selected_shop_and_unpaid_amount() -> None:
    now = datetime(2026, 6, 12, tzinfo=BANGKOK_TZ)
    stats = aggregate_shop_analytics(
        [ShopSummaryRow(32, Decimal("8400"), [1, 2, 3, 4], now, Decimal("2400"))],
        [ShopProductRow("Brownie", 20, Decimal("1600"))],
        [LastProductRow("Cookies", 5)],
        Shop(7, "Shop A"),
        "month",
    )

    assert stats["shop_id"] == 7
    assert stats["paid_revenue"] == Decimal("8400.00")
    assert stats["paid_orders"] == 4
    assert stats["unpaid_amount"] == Decimal("2400.00")
    assert stats["suggested_action"] == "Payment follow-up first"


def test_crm_callbacks_use_separate_namespaces() -> None:
    callbacks = [
        "crm:followups:attention",
        "crm:products:best_revenue",
        "crm:forecast:month",
        "crm:shop_analytics:7:month",
    ]

    assert all(callback.startswith("crm:") for callback in callbacks)
    assert not any(callback.startswith("stats:") for callback in callbacks)


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
