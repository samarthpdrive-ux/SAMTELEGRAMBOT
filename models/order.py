"""
models/order.py

Order model — matches the `orders` table exactly, including the
columns added by migrate_add_columns.py (quantity, delivery_type,
is_preorder).

Written in SQLAlchemy 2.x declarative style (Mapped / mapped_column)
instead of the legacy Column(...) style, per project convention.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
    )

    product_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    product_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # DECIMAL(20, 8) — never Float. Every read of this column comes
    # back as a Python Decimal; never mix it with float in arithmetic.
    amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0"),
    )

    quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    # "automatic" | "manual" | "hybrid" — mirrors Product.delivery_type
    # at the time of purchase, so a later change to the product doesn't
    # rewrite the history of how this specific order was fulfilled.
    delivery_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="automatic",
        server_default="automatic",
    )

    is_preorder: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )

    # "completed" | "pending_manual" | "preorder" | "refunded"
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="completed",
        server_default="completed",
    )

    refunded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )

    delivered_account: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} telegram_id={self.telegram_id} "
            f"product_id={self.product_id} qty={self.quantity} "
            f"status={self.status!r}>"
        )
