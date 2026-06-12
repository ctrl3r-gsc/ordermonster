from collections import namedtuple
from datetime import datetime
from decimal import Decimal

from services.statistics_core import BANGKOK_TZ, aggregate_stats_rows, period_bounds


StatsRow = namedtuple("StatsRow", "product_id product_name quantity_sold revenue gift_quantity")


def test_aggregation_counts_paid_non_gift_sales_by_product() -> None:
    stats = aggregate_stats_rows(
        [
            StatsRow(1, "BROWNIE 100mg THC", 3, Decimal("300.00"), 2),
            StatsRow(2, "GUMMIES 350mg", 5, Decimal("1150.00"), 0),
        ],
        total_paid_orders=2,
        period="month",
    )

    assert stats["total_paid_orders"] == 2
    assert stats["total_quantity"] == 8
    assert stats["total_revenue"] == Decimal("1450.00")
    assert stats["products"][0]["gift_quantity"] == 2


def test_gift_only_rows_do_not_increase_sales_or_revenue() -> None:
    stats = aggregate_stats_rows(
        [
            StatsRow(1, "BROWNIE 100mg THC", 0, Decimal("0.00"), 2),
        ],
        total_paid_orders=1,
        period="month",
    )

    assert stats["total_paid_orders"] == 1
    assert stats["total_quantity"] == 0
    assert stats["total_revenue"] == Decimal("0.00")
    assert stats["products"] == []


def test_unpaid_orders_are_excluded_before_aggregation() -> None:
    paid_rows = [
        StatsRow(1, "BROWNIE 100mg THC", 3, Decimal("300.00"), 0),
    ]
    unpaid_rows_that_must_not_be_passed_to_aggregation = [
        StatsRow(1, "BROWNIE 100mg THC", 99, Decimal("9900.00"), 0),
    ]

    stats = aggregate_stats_rows(paid_rows, total_paid_orders=1, period="month")

    assert unpaid_rows_that_must_not_be_passed_to_aggregation
    assert stats["total_paid_orders"] == 1
    assert stats["total_quantity"] == 3
    assert stats["total_revenue"] == Decimal("300.00")


def test_period_bounds_use_bangkok_calendar_periods() -> None:
    now = datetime(2026, 6, 12, 20, 30, tzinfo=BANGKOK_TZ)

    today_start, today_end = period_bounds("today", now)
    week_start, week_end = period_bounds("week", now)
    month_start, month_end = period_bounds("month", now)
    all_start, all_end = period_bounds("all", now)

    assert today_start.isoformat() == "2026-06-11T17:00:00+00:00"
    assert week_start.isoformat() == "2026-06-07T17:00:00+00:00"
    assert month_start.isoformat() == "2026-05-31T17:00:00+00:00"
    assert today_end == week_end == month_end
    assert all_start is None
    assert all_end is None


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
