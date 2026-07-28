from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    Boolean,
    DateTime,
    DECIMAL
)
from sqlalchemy.sql import func

from database import Base


class Order(Base):
    __tablename__ = "orders"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    telegram_id = Column(
        BigInteger,
        nullable=False,
        index=True
    )

    product_id = Column(
        Integer,
        nullable=False
    )

    product_name = Column(
        String(255),
        nullable=False
    )

    amount = Column(
        DECIMAL(20, 8),
        nullable=False
    )

    quantity = Column(
        Integer,
        default=1,
        nullable=False
    )

    delivery_type = Column(
        String(20),
        default="automatic"
    )

    is_preorder = Column(
        Boolean,
        default=False
    )

    status = Column(
        String(50),
        default="completed"
    )

    refunded = Column(
        Boolean,
        default=False
    )

    delivered_account = Column(
        Text
    )

    created_at = Column(
        DateTime,
        server_default=func.now()
    )
