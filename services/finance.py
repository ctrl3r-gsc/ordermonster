from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import (
    CompanyTransaction,
    CompanyTransactionPaymentMethod,
    CompanyTransactionSourceBot,
    CompanyTransactionType,
    Order,
    OrderPayment,
    PaymentMethod,
)
from services.orders import decimal_money, paid_amount


SALES_CATEGORY = "sales"
DEFAULT_CURRENCY = "THB"


@dataclass
class FinanceBackfillSummary:
    checked: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0


def finance_payment_method(method: PaymentMethod | str | None) -> CompanyTransactionPaymentMethod:
    raw_method = method.value if isinstance(method, PaymentMethod) else method
    if raw_method == "cash":
        return CompanyTransactionPaymentMethod.cash
    if raw_method in {"transaction", "transfer"}:
        return CompanyTransactionPaymentMethod.transfer
    if raw_method == "crypto":
        return CompanyTransactionPaymentMethod.crypto
    return CompanyTransactionPaymentMethod.unknown


def order_finance_payment_method(order: Order) -> CompanyTransactionPaymentMethod:
    latest_payment = latest_order_payment(order)
    if latest_payment is None:
        return CompanyTransactionPaymentMethod.unknown
    return finance_payment_method(latest_payment.payment_method)


def latest_order_payment(order: Order) -> OrderPayment | None:
    if not order.payments:
        return None
    return max(
        order.payments,
        key=lambda payment: (
            payment.created_at or datetime.min.replace(tzinfo=timezone.utc),
            payment.id or 0,
        ),
    )


def order_income_transaction_date(order: Order) -> datetime:
    latest_payment = latest_order_payment(order)
    if latest_payment is not None and latest_payment.created_at:
        return latest_payment.created_at
    return order.updated_at or order.created_at or datetime.now(timezone.utc)


async def order_income_transaction(session: AsyncSession, order_id: int) -> CompanyTransaction | None:
    return await session.scalar(
        select(CompanyTransaction).where(
            CompanyTransaction.source_bot == CompanyTransactionSourceBot.ordermonster,
            CompanyTransaction.related_order_id == order_id,
            CompanyTransaction.type == CompanyTransactionType.income,
        )
    )


async def sync_order_income_transaction(session: AsyncSession, order: Order) -> CompanyTransaction | None:
    if "payments" not in order.__dict__:
        order = await session.scalar(
            select(Order)
            .where(Order.id == order.id)
            .options(selectinload(Order.payments))
        )
    if order is None:
        return None

    current_paid = decimal_money(paid_amount(order))
    existing = await order_income_transaction(session, order.id)
    if current_paid <= 0:
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        return None

    transaction_date = order_income_transaction_date(order)
    payment_method = order_finance_payment_method(order)
    amount = current_paid

    if existing is None:
        transaction = CompanyTransaction(
            type=CompanyTransactionType.income,
            source_bot=CompanyTransactionSourceBot.ordermonster,
            category=SALES_CATEGORY,
            amount=amount,
            currency=DEFAULT_CURRENCY,
            payment_method=payment_method,
            related_order_id=order.id,
            description=f"Order #{order.display_number or order.id}",
            transaction_date=transaction_date,
        )
        session.add(transaction)
        await session.flush()
        return transaction

    existing.category = SALES_CATEGORY
    existing.amount = amount
    existing.currency = DEFAULT_CURRENCY
    existing.payment_method = payment_method
    existing.description = f"Order #{order.display_number or order.id}"
    existing.transaction_date = transaction_date
    existing.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return existing


async def add_expense_transaction(
    session: AsyncSession,
    *,
    category: str,
    amount: Decimal | int | str,
    payment_method: str | None = None,
    description: str | None = None,
    transaction_date: datetime | None = None,
    currency: str = DEFAULT_CURRENCY,
    source_bot: CompanyTransactionSourceBot = CompanyTransactionSourceBot.expense_bot,
) -> CompanyTransaction:
    clean_category = category.strip()
    clean_amount = decimal_money(amount)
    if not clean_category:
        raise ValueError("Expense category cannot be empty")
    if clean_amount <= 0:
        raise ValueError("Expense amount must be positive")
    transaction = CompanyTransaction(
        type=CompanyTransactionType.expense,
        source_bot=source_bot,
        category=clean_category,
        amount=clean_amount,
        currency=(currency or DEFAULT_CURRENCY).upper(),
        payment_method=finance_payment_method(payment_method),
        description=(description or None),
        transaction_date=transaction_date or datetime.now(timezone.utc),
    )
    session.add(transaction)
    await session.flush()
    return transaction


async def monthly_financial_summary(session: AsyncSession, year: int, month: int) -> dict:
    _, days_in_month = calendar.monthrange(year, month)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + int(month == 12), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)

    transactions = list(
        (
            await session.scalars(
                select(CompanyTransaction).where(
                    CompanyTransaction.transaction_date >= start,
                    CompanyTransaction.transaction_date < end,
                )
            )
        ).all()
    )

    income = sum(
        (decimal_money(tx.amount) for tx in transactions if tx.type == CompanyTransactionType.income),
        Decimal("0.00"),
    )
    expenses = sum(
        (decimal_money(tx.amount) for tx in transactions if tx.type == CompanyTransactionType.expense),
        Decimal("0.00"),
    )
    by_payment_method: dict[str, dict[str, Decimal]] = {}
    by_category: dict[str, dict[str, Decimal]] = {}
    for tx in transactions:
        amount = decimal_money(tx.amount)
        bucket = "income" if tx.type == CompanyTransactionType.income else "expenses"
        method_key = tx.payment_method.value
        category_key = tx.category
        by_payment_method.setdefault(method_key, {"income": Decimal("0.00"), "expenses": Decimal("0.00")})
        by_category.setdefault(category_key, {"income": Decimal("0.00"), "expenses": Decimal("0.00")})
        by_payment_method[method_key][bucket] += amount
        by_category[category_key][bucket] += amount

    def finalize_breakdown(source: dict[str, dict[str, Decimal]]) -> dict[str, dict[str, Decimal]]:
        return {
            key: {
                "income": values["income"].quantize(Decimal("0.01")),
                "expenses": values["expenses"].quantize(Decimal("0.01")),
                "profit": (values["income"] - values["expenses"]).quantize(Decimal("0.01")),
            }
            for key, values in source.items()
        }

    return {
        "year": year,
        "month": month,
        "period_start": start,
        "period_end": datetime(year, month, days_in_month, 23, 59, 59, tzinfo=timezone.utc),
        "income": income.quantize(Decimal("0.01")),
        "expenses": expenses.quantize(Decimal("0.01")),
        "profit": (income - expenses).quantize(Decimal("0.01")),
        "breakdown_by_payment_method": finalize_breakdown(by_payment_method),
        "breakdown_by_category": finalize_breakdown(by_category),
    }


async def backfill_ordermonster_income_transactions(session: AsyncSession) -> FinanceBackfillSummary:
    summary = FinanceBackfillSummary()
    orders = list(
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
    for order in orders:
        summary.checked += 1
        current_paid = decimal_money(paid_amount(order))
        existing = await order_income_transaction(session, order.id)
        if current_paid <= 0:
            summary.skipped += 1
            continue

        await sync_order_income_transaction(session, order)
        if existing is None:
            summary.created += 1
        else:
            summary.updated += 1

    await session.flush()
    return summary
