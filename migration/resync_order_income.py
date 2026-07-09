from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db.models import CompanyTransaction, CompanyTransactionSourceBot, CompanyTransactionType, Order, OrderPayment
from services.finance import DEFAULT_CURRENCY, SALES_CATEGORY, order_income_transaction_date, order_finance_payment_method
from services.orders import decimal_money, paid_amount, remaining_amount


@dataclass
class ResyncSummary:
    orders_scanned: int = 0
    orders_with_payments: int = 0
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


async def load_orders_with_payments(session: AsyncSession) -> list[Order]:
    return list(
        (
            await session.scalars(
                select(Order)
                .join(OrderPayment, OrderPayment.order_id == Order.id)
                .options(selectinload(Order.payments))
                .distinct()
                .order_by(Order.id.asc())
            )
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


async def resync_order_income(session: AsyncSession, *, dry_run: bool = False) -> ResyncSummary:
    summary = ResyncSummary()
    summary.orders_scanned = int(await session.scalar(select(func.count(Order.id))) or 0)
    orders = await load_orders_with_payments(session)

    for order in orders:
        fields = build_income_fields(order)
        if fields.amount <= 0:
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
                f"{'[dry-run] would create' if dry_run else 'create'}: "
                f"{label}, received={fields.amount} THB, remaining={fields.remaining_amount} THB"
            )
            if not dry_run:
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
                f"{'[dry-run] would update' if dry_run else 'update'}: "
                f"{label}, transaction_id={transaction.id}, "
                f"amount={decimal_money(transaction.amount)} -> {fields.amount} THB, "
                f"remaining={fields.remaining_amount} THB"
            )
            if not dry_run:
                apply_income_fields(transaction, fields)
        else:
            summary.transactions_unchanged += 1

    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return summary


def print_summary(summary: ResyncSummary) -> None:
    print(f"Orders scanned: {summary.orders_scanned}")
    print(f"Orders with payments: {summary.orders_with_payments}")
    print(f"Transactions created: {summary.transactions_created}")
    print(f"Transactions updated: {summary.transactions_updated}")
    print(f"Transactions unchanged: {summary.transactions_unchanged}")
    print(f"Skipped/warnings: {summary.warnings}")
    print(f"Total synced amount: {decimal_money(summary.total_synced_amount)} THB")


async def main_async(dry_run: bool) -> None:
    from db import SessionLocal

    async with SessionLocal() as session:
        summary = await resync_order_income(session, dry_run=dry_run)
    print_summary(summary)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resync OrderMonster order payments into company_transactions.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without committing them.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main_async(dry_run=args.dry_run))
