from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db.models import (
    CompanyTransaction,
    CompanyTransactionSourceBot,
    CompanyTransactionType,
    DeliveryStatus,
    Order,
    OrderPayment,
    PaymentStatus,
)
from services.finance import DEFAULT_CURRENCY, SALES_CATEGORY, order_income_transaction_date, order_finance_payment_method
from services.orders import decimal_money, paid_amount, remaining_amount


@dataclass
class ResyncSummary:
    orders_scanned: int = 0
    orders_with_payments: int = 0
    closed_paid_delivered_skipped: int = 0
    only_partial_skipped: int = 0
    transactions_created: int = 0
    transactions_updated: int = 0
    transactions_unchanged: int = 0
    warnings: int = 0
    total_synced_amount: Decimal = Decimal("0.00")


@dataclass
class IncomeFields:
    category: str
    amount: Decimal
    currency: str
    payment_method: object
    related_order_id: int
    description: str
    transaction_date: datetime
    remaining_amount: Decimal


@dataclass(frozen=True)
class ResyncOptions:
    apply: bool = False
    only_partial: bool = False
    display_numbers: frozenset[int] = frozenset()
    include_closed_paid_delivered: bool = False

    @property
    def dry_run(self) -> bool:
        return not self.apply

    @property
    def has_safe_filter(self) -> bool:
        return self.only_partial or bool(self.display_numbers)


def parse_display_numbers(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    display_numbers: set[int] = set()
    for chunk in raw.split(","):
        value = chunk.strip()
        if not value:
            continue
        try:
            display_number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Invalid display number: {value}") from exc
        if display_number <= 0:
            raise argparse.ArgumentTypeError(f"Display number must be positive: {value}")
        display_numbers.add(display_number)
    if not display_numbers:
        raise argparse.ArgumentTypeError("At least one display number is required.")
    return frozenset(display_numbers)


def validate_options(options: ResyncOptions) -> str | None:
    if options.apply and not options.has_safe_filter and not options.include_closed_paid_delivered:
        return (
            "Refusing broad apply without a safe filter. Use --only-partial or --display-numbers, "
            "or explicitly pass --include-closed-paid-delivered."
        )
    return None


def describe_filters(options: ResyncOptions) -> str:
    filters = []
    if options.only_partial:
        filters.append("only_partial")
    if options.display_numbers:
        values = ",".join(str(value) for value in sorted(options.display_numbers))
        filters.append(f"display_numbers={values}")
    if options.include_closed_paid_delivered:
        filters.append("include_closed_paid_delivered")
    return ", ".join(filters) if filters else "none"


def build_income_fields(order: Order) -> IncomeFields:
    paid = decimal_money(paid_amount(order))
    return IncomeFields(
        category=SALES_CATEGORY,
        amount=paid,
        currency=DEFAULT_CURRENCY,
        payment_method=order_finance_payment_method(order),
        related_order_id=order.id,
        description=f"Order #{order.display_number or order.id}",
        transaction_date=order_income_transaction_date(order),
        remaining_amount=decimal_money(remaining_amount(order)),
    )


def is_closed_paid_delivered(order: Order, fields: IncomeFields) -> bool:
    total = decimal_money(order.total_amount)
    return (
        order.payment_status == PaymentStatus.paid
        and order.delivery_status == DeliveryStatus.delivered
        and total > 0
        and fields.amount >= total
    )


def sync_skip_reason(order: Order, fields: IncomeFields, options: ResyncOptions) -> str | None:
    if is_closed_paid_delivered(order, fields) and not options.include_closed_paid_delivered:
        return "closed_paid_delivered"
    if options.only_partial and fields.remaining_amount <= 0:
        return "not_partial"
    return None


def transaction_needs_update(transaction: CompanyTransaction, fields: IncomeFields) -> bool:
    return any(
        (
            transaction.category != fields.category,
            decimal_money(transaction.amount) != fields.amount,
            transaction.currency != fields.currency,
            transaction.payment_method != fields.payment_method,
            transaction.description != fields.description,
            transaction.transaction_date != fields.transaction_date,
        )
    )


async def load_orders_with_payments(session: AsyncSession, options: ResyncOptions) -> list[Order]:
    stmt = (
        select(Order)
        .join(OrderPayment, OrderPayment.order_id == Order.id)
        .options(selectinload(Order.payments))
        .distinct()
        .order_by(Order.id.asc())
    )
    if options.display_numbers:
        stmt = stmt.where(Order.display_number.in_(sorted(options.display_numbers)))
    return list(
        (
            await session.scalars(stmt)
        ).all()
    )


async def load_income_transactions(session: AsyncSession, order_id: int) -> list[CompanyTransaction]:
    return list(
        (
            await session.scalars(
                select(CompanyTransaction)
                .where(
                    CompanyTransaction.source_bot == CompanyTransactionSourceBot.ordermonster,
                    CompanyTransaction.related_order_id == order_id,
                    CompanyTransaction.type == CompanyTransactionType.income,
                )
                .order_by(CompanyTransaction.id.asc())
            )
        ).all()
    )


def apply_income_fields(transaction: CompanyTransaction, fields: IncomeFields) -> None:
    transaction.category = fields.category
    transaction.amount = fields.amount
    transaction.currency = fields.currency
    transaction.payment_method = fields.payment_method
    transaction.description = fields.description
    transaction.transaction_date = fields.transaction_date
    transaction.updated_at = datetime.now(timezone.utc)


async def resync_order_income(session: AsyncSession, *, options: ResyncOptions) -> ResyncSummary:
    summary = ResyncSummary()
    orders = await load_orders_with_payments(session, options)
    summary.orders_scanned = len(orders)

    for order in orders:
        fields = build_income_fields(order)
        if fields.amount <= 0:
            continue

        skip_reason = sync_skip_reason(order, fields, options)
        if skip_reason == "closed_paid_delivered":
            summary.closed_paid_delivered_skipped += 1
            continue
        if skip_reason == "not_partial":
            summary.only_partial_skipped += 1
            continue

        summary.orders_with_payments += 1
        summary.total_synced_amount += fields.amount
        transactions = await load_income_transactions(session, order.id)
        label = f"Order #{order.display_number or order.id}"

        if len(transactions) > 1:
            summary.warnings += 1
            print(
                f"WARNING: {label} has {len(transactions)} ordermonster income transactions; "
                "updating the first one and leaving duplicates untouched."
            )

        if not transactions:
            summary.transactions_created += 1
            print(
                f"{'[dry-run] would create' if options.dry_run else 'create'}: "
                f"{label}, received={fields.amount} THB, remaining={fields.remaining_amount} THB"
            )
            if options.apply:
                session.add(
                    CompanyTransaction(
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
                )
            continue

        transaction = transactions[0]
        if transaction_needs_update(transaction, fields):
            summary.transactions_updated += 1
            print(
                f"{'[dry-run] would update' if options.dry_run else 'update'}: "
                f"{label}, transaction_id={transaction.id}, "
                f"amount={decimal_money(transaction.amount)} -> {fields.amount} THB, "
                f"remaining={fields.remaining_amount} THB"
            )
            if options.apply:
                apply_income_fields(transaction, fields)
        else:
            summary.transactions_unchanged += 1

    if options.dry_run:
        await session.rollback()
    else:
        await session.commit()
    return summary


def print_summary(summary: ResyncSummary, options: ResyncOptions) -> None:
    print(f"Mode: {'apply' if options.apply else 'dry-run'}")
    print(f"Filters used: {describe_filters(options)}")
    print(f"Orders scanned: {summary.orders_scanned}")
    print(f"Orders with payments: {summary.orders_with_payments}")
    print(f"Orders skipped because closed paid delivered: {summary.closed_paid_delivered_skipped}")
    print(f"Orders skipped by only-partial filter: {summary.only_partial_skipped}")
    print(f"Transactions created: {summary.transactions_created}")
    print(f"Transactions updated: {summary.transactions_updated}")
    print(f"Transactions unchanged: {summary.transactions_unchanged}")
    print(f"Skipped/warnings: {summary.warnings}")
    print(f"Total synced amount: {decimal_money(summary.total_synced_amount)} THB")


async def main_async(options: ResyncOptions) -> int:
    error = validate_options(options)
    if error:
        print(error)
        return 2
    if options.include_closed_paid_delivered:
        print("WARNING: this may increase accounting bank balance by adding historical closed orders.")
    from db import SessionLocal

    async with SessionLocal() as session:
        summary = await resync_order_income(session, options=options)
    print_summary(summary, options)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resync OrderMonster order payments into company_transactions.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print planned changes without committing them. This is the default.")
    mode.add_argument("--apply", action="store_true", help="Commit selected changes to the database.")
    parser.add_argument("--only-partial", action="store_true", help="Sync only orders where received amount is above 0 and below total.")
    parser.add_argument("--display-numbers", type=parse_display_numbers, help="Comma-separated order display numbers, for example 52,56.")
    parser.add_argument(
        "--include-closed-paid-delivered",
        action="store_true",
        help="Dangerous: allow historical paid+delivered orders to be synced.",
    )
    return parser.parse_args()


def options_from_args(args: argparse.Namespace) -> ResyncOptions:
    return ResyncOptions(
        apply=bool(args.apply),
        only_partial=bool(args.only_partial),
        display_numbers=args.display_numbers or frozenset(),
        include_closed_paid_delivered=bool(args.include_closed_paid_delivered),
    )


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(asyncio.run(main_async(options_from_args(args))))
