from sqlalchemy import *
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
        BigInteger
    )

    product_id = Column(
        Integer
    )

    product_name = Column(
        String(255)
    )

    delivered_account = Column(
        Text
    )

    amount = Column(
        DECIMAL(20, 8)
    )

    status = Column(
        String(50),
        default="completed"
    )

    refunded = Column(
        Boolean,
        default=False
    )

    created_at = Column(
        DateTime,
        server_default=func.now()
    )
