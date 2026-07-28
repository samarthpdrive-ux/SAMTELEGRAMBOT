from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Numeric,
    Boolean,
)

from database import Base


class User(Base):
    __tablename__ = "users"

    # -----------------------------------------
    # Primary Key
    # -----------------------------------------

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        index=True,
    )

    # -----------------------------------------
    # Telegram
    # -----------------------------------------

    telegram_id = Column(
        BigInteger,
        unique=True,
        nullable=False,
        index=True,
    )

    username = Column(
        String(100),
        nullable=True,
    )

    full_name = Column(
        String(255),
        nullable=False,
    )

    # -----------------------------------------
    # Wallet
    # -----------------------------------------

    balance = Column(
        Numeric(20, 8),
        nullable=False,
        default=0,
    )

    total_deposited = Column(
        Numeric(20, 8),
        nullable=False,
        default=0,
    )

    total_spent = Column(
        Numeric(20, 8),
        nullable=False,
        default=0,
    )

    # -----------------------------------------
    # Referral
    # -----------------------------------------

    referral_code = Column(
        String(100),
        unique=True,
        nullable=True,
        index=True,
    )

    referred_by = Column(
        BigInteger,
        nullable=True,
        index=True,
    )

    total_referrals = Column(
        Integer,
        nullable=False,
        default=0,
    )

    referral_earnings = Column(
        Numeric(20, 8),
        nullable=False,
        default=0,
    )

    # -----------------------------------------
    # Statistics
    # -----------------------------------------

    total_orders = Column(
        Integer,
        nullable=False,
        default=0,
    )

    # -----------------------------------------
    # Status
    # -----------------------------------------

    is_banned = Column(
        Boolean,
        nullable=False,
        default=False,
    )

    # -----------------------------------------
    # Helper Methods
    # -----------------------------------------

    @property
    def balance_float(self):
        return float(self.balance or 0)

    @property
    def spent_float(self):
        return float(self.total_spent or 0)

    @property
    def deposited_float(self):
        return float(self.total_deposited or 0)

    @property
    def referral_float(self):
        return float(self.referral_earnings or 0)

    def __repr__(self):
        return (
            f"<User("
            f"id={self.id}, "
            f"telegram_id={self.telegram_id}, "
            f"balance={self.balance}"
            f")>"
        )
