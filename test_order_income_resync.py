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
    from migration.resync_order_income import build_income_fields, transaction_needs_update
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


def make_order(*, total: str = "8350.00", paid: str = "8100.00") -> Order:
    paid_at = datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc)
    order = Order(
        id=12,
        display_number=34,
        user_id=99,
        shop_id=1,
        total_amount=Decimal(total),
        payment_status=PaymentStatus.partially_paid,
        delivery_status=DeliveryStatus.shipped,
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
