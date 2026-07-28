from sqlalchemy import *
from database import Base


class Product(Base):
    __tablename__ = "products"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    name = Column(
        String(255),
        nullable=False
    )

    icon = Column(
        String(20),
        default="📦"
    )

    description = Column(
        Text
    )

    category = Column(
        String(255)
    )

    price = Column(
        DECIMAL(20, 8),
        default=0
    )

    stock = Column(
        Integer,
        default=0
    )

    file_content = Column(
        Text
    )

    is_active = Column(
        Boolean,
        default=True
    )

    # "automatic" -> deliver instantly from file_content accounts
    # "manual"    -> admin fulfills each order by hand from the Orders panel
    # "hybrid"    -> auto-deliver if an account is available, else queue for manual fulfillment
    delivery_type = Column(
        String(20),
        default="automatic"
    )

    # If True, customers can still order at 0 stock; the order is queued
    # as a preorder instead of being blocked.
    preorder = Column(
        Boolean,
        default=False
    )

    # Admins get a Telegram alert once stock drops to/below this number.
    low_stock_threshold = Column(
        Integer,
        default=3
    )
