# models/user.py

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Float
)

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(
        Integer,
        primary_key=True,
        index=True
    )

    # Telegram Information
    telegram_id = Column(
        BigInteger,
        unique=True,
        nullable=False
    )

    username = Column(
        String(100),
        nullable=True
    )

    full_name = Column(
        String(255),
        nullable=False
    )

    # Wallet
    balance = Column(
        Float,
        default=0.0
    )

    # Referral System
    referral_code = Column(
        String(100),
        unique=True,
        nullable=True
    )

    referred_by = Column(
        BigInteger,
        nullable=True
    )

    total_referrals = Column(
        Integer,
        default=0
    )

    referral_earnings = Column(
        Float,
        default=0.0
    )

    # Statistics
    total_orders = Column(
        Integer,
        default=0
    )

    total_spent = Column(
        Float,
        default=0.0
    )

    total_deposited = Column(
        Float,
        default=0.0
    )

    # User Status
    is_banned = Column(
        Integer,
        default=0
    )