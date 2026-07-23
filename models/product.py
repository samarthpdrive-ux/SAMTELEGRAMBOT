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