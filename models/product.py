"""
models/product.py

Product model — SQLAlchemy 2.x declarative style. Columns are
unchanged from the previous version (verified against
migrate_add_columns.py, which is where delivery_type, preorder, and
low_stock_threshold were added to an already-live table).
"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    icon: Mapped[Optional[str]] = mapped_column(
        String(20),
        default="📦",
    )

    description: Mapped[Optional[str]] = mapped_column(Text)

    category: Mapped[Optional[str]] = mapped_column(String(255))

    # DECIMAL(20, 8) — always compare/multiply as Decimal, never float.
    price: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0"),
    )

    # Manual-fulfillment / hybrid stock counter. For "automatic"
    # delivery, real availability is len(file_content accounts), not
    # this column — see _accounts_count()/_real_stock() in
    # handlers/products.py.
    stock: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # Newline-separated pool of account/key strings consumed on
    # automatic/hybrid delivery.
    file_content: Mapped[Optional[str]] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    # "automatic" -> deliver instantly from file_content accounts
    # "manual"    -> admin fulfills each order by hand from the Orders panel
    # "hybrid"    -> auto-deliver if an account is available, else queue
    #                for manual fulfillment
    delivery_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="automatic",
        server_default="automatic",
    )

    # If True, customers can still order at 0 stock; the order is
    # queued as a preorder instead of being blocked.
    preorder: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )

    # Admins get a Telegram alert once stock drops to/below this number.
    low_stock_threshold: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3,
        server_default="3",
    )

    def __repr__(self) -> str:
        return f"<Product id={self.id} name={self.name!r} stock={self.stock}>"
