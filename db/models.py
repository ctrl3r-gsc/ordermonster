import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class DeliveryStatus(str, enum.Enum):
    pending_shipment = "pending_shipment"
    shipped = "shipped"
    delivered = "delivered"


class PaymentStatus(str, enum.Enum):
    unpaid = "unpaid"
    partially_paid = "partially_paid"
    paid = "paid"


class PaymentMethod(str, enum.Enum):
    cash = "cash"
    transaction = "transaction"
    crypto = "crypto"


class CompanyTransactionType(str, enum.Enum):
    income = "income"
    expense = "expense"


class CompanyTransactionSourceBot(str, enum.Enum):
    ordermonster = "ordermonster"
    expense_bot = "expense_bot"
    manual = "manual"


class CompanyTransactionPaymentMethod(str, enum.Enum):
    cash = "cash"
    transfer = "transfer"
    crypto = "crypto"
    unknown = "unknown"


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    price_modifier: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    orders: Mapped[list["Order"]] = relationship(back_populates="shop")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("sku", name="uq_products_sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    dosage: Mapped[int | None] = mapped_column(Integer)
    flavor: Mapped[str | None] = mapped_column(String(120))
    potency_type: Mapped[str | None] = mapped_column(String(50))
    sku: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    items: Mapped[list["OrderItem"]] = relationship(back_populates="product")
    aliases: Mapped[list["ProductAlias"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class ProductAlias(Base):
    __tablename__ = "product_aliases"
    __table_args__ = (UniqueConstraint("product_id", "alias", name="uq_product_aliases_product_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(255), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    product: Mapped[Product] = relationship(back_populates="aliases")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_number: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status"), default=DeliveryStatus.pending_shipment
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), default=PaymentStatus.unpaid, index=True
    )
    tracking_number: Mapped[str | None] = mapped_column(Text)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    shop: Mapped[Shop] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    payments: Mapped[list["OrderPayment"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    company_transactions: Mapped[list["CompanyTransaction"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    price_per_unit: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    is_gift: Mapped[bool] = mapped_column(Boolean, default=False)

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(back_populates="items")


class OrderPayment(Base):
    __tablename__ = "order_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    payment_method: Mapped[PaymentMethod] = mapped_column(Enum(PaymentMethod, name="payment_method"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Order] = relationship(back_populates="payments")


class CompanyTransaction(Base):
    __tablename__ = "company_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[CompanyTransactionType] = mapped_column(Enum(CompanyTransactionType, name="company_transaction_type"), index=True)
    source_bot: Mapped[CompanyTransactionSourceBot] = mapped_column(
        Enum(CompanyTransactionSourceBot, name="company_transaction_source_bot"), index=True
    )
    category: Mapped[str] = mapped_column(String(120), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(10), default="THB", server_default="THB")
    payment_method: Mapped[CompanyTransactionPaymentMethod] = mapped_column(
        Enum(CompanyTransactionPaymentMethod, name="company_transaction_payment_method"),
        default=CompanyTransactionPaymentMethod.unknown,
        index=True,
    )
    related_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    transaction_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    order: Mapped[Order | None] = relationship(back_populates="company_transactions")


Index(
    "uq_company_transactions_ordermonster_order",
    CompanyTransaction.source_bot,
    CompanyTransaction.related_order_id,
    unique=True,
    postgresql_where=(
        (CompanyTransaction.source_bot == CompanyTransactionSourceBot.ordermonster)
        & (CompanyTransaction.type == CompanyTransactionType.income)
        & (CompanyTransaction.related_order_id.is_not(None))
    ),
)
