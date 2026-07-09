from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

try:
    from db.models import (
        CompanyTransaction,
        CompanyTransactionPaymentMethod,
        CompanyTransactionSourceBot,
        CompanyTransactionType,
        DeliveryStatus,
        Order,
        OrderPayment,
        PaymentMethod,
        PaymentStatus,
    )
    from migration.resync_order_income import (
        ResyncOptions,
        build_income_fields,
        parse_display_numbers,
        sync_skip_reason,
        transaction_needs_update,
        validate_options,
    )
except ModuleNotFoundError as exc:
    if exc.name != "sqlalchemy":
        raise
    SQLALCHEMY_AVAILABLE = False
else:
    SQLALCHEMY_AVAILABLE = True


def require_sqlalchemy() -> bool:
    if SQLALCHEMY_AVAILABLE:
        return True
    print("Skipped order income resync test: SQLAlchemy is not installed in this local environment.")
    return False


def make_order(
    *,
    display_number: int = 34,
    total: str = "8350.00",
    paid: str = "8100.00",
    payment_status=None,
    delivery_status=None,
) -> Order:
    paid_at = datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc)
    payment_status = payment_status or PaymentStatus.partially_paid
    delivery_status = delivery_status or DeliveryStatus.shipped
    order = Order(
        id=12,
        display_number=display_number,
        user_id=99,
        shop_id=1,
        total_amount=Decimal(total),
        payment_status=payment_status,
        delivery_status=delivery_status,
    )
    order.payments = [
        OrderPayment(
            id=1,
            order_id=order.id,
            payment_method=PaymentMethod.transaction,
            amount=Decimal(paid),
            created_at=paid_at,
        )
    ]
    return order


def test_default_options_are_dry_run() -> None:
    if not require_sqlalchemy():
        return
    options = ResyncOptions()

    assert options.dry_run is True
    assert options.apply is False


def test_broad_apply_without_safe_filter_is_refused() -> None:
    if not require_sqlalchemy():
        return
    error = validate_options(ResyncOptions(apply=True))

    assert error is not None
    assert "Refusing broad apply" in error


def test_display_numbers_parser_accepts_comma_separated_targets() -> None:
    if not require_sqlalchemy():
        return
    assert parse_display_numbers("52,56") == frozenset({52, 56})


def make_synced_transaction(order: Order) -> CompanyTransaction:
    fields = build_income_fields(order)
    return CompanyTransaction(
        id=5,
        type=CompanyTransactionType.income,
        source_bot=CompanyTransactionSourceBot.ordermonster,
        category=fields.category,
        amount=fields.amount,
        currency=fields.currency,
        payment_method=fields.payment_method,
        related_order_id=fields.related_order_id,
        description=fields.description,
        transaction_date=fields.transaction_date,
    )


def test_partial_payment_builds_income_fields_without_closing_order() -> None:
    if not require_sqlalchemy():
        return
    order = make_order()

    fields = build_income_fields(order)

    assert fields.amount == Decimal("8100.00")
    assert fields.remaining_amount == Decimal("250.00")
    assert fields.payment_method == CompanyTransactionPaymentMethod.transfer
    assert fields.description == "Order #34"
    assert order.payment_status == PaymentStatus.partially_paid
    assert order.delivery_status == DeliveryStatus.shipped


def test_only_partial_apply_skips_fully_paid_orders() -> None:
    if not require_sqlalchemy():
        return
    order = make_order(total="8350.00", paid="8350.00", payment_status=PaymentStatus.paid)
    fields = build_income_fields(order)

    assert sync_skip_reason(order, fields, ResyncOptions(apply=True, only_partial=True)) == "not_partial"


def test_old_paid_delivered_fully_paid_order_is_skipped_by_default() -> None:
    if not require_sqlalchemy():
        return
    order = make_order(
        total="8350.00",
        paid="8350.00",
        payment_status=PaymentStatus.paid,
        delivery_status=DeliveryStatus.delivered,
    )
    fields = build_income_fields(order)

    assert sync_skip_reason(order, fields, ResyncOptions()) == "closed_paid_delivered"
    assert sync_skip_reason(order, fields, ResyncOptions(include_closed_paid_delivered=True)) is None


def test_synced_transaction_is_idempotent() -> None:
    if not require_sqlalchemy():
        return
    order = make_order()
    transaction = make_synced_transaction(order)

    assert transaction_needs_update(transaction, build_income_fields(order)) is False


def test_later_payment_updates_income_amount_to_received_total() -> None:
    if not require_sqlalchemy():
        return
    order = make_order()
    transaction = make_synced_transaction(order)
    order.payments.append(
        OrderPayment(
            id=2,
            order_id=order.id,
            payment_method=PaymentMethod.cash,
            amount=Decimal("250.00"),
            created_at=datetime(2026, 7, 9, 11, 0, tzinfo=timezone.utc),
        )
    )

    fields = build_income_fields(order)

    assert fields.amount == Decimal("8350.00")
    assert fields.remaining_amount == Decimal("0.00")
    assert fields.payment_method == CompanyTransactionPaymentMethod.cash
    assert transaction_needs_update(transaction, fields) is True


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
