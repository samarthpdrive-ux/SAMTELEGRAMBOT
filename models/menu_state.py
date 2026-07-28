# models/menu_state.py
#
# Tracks the last "menu" message sent to each user (chat_id + message_id).
# Used to delete the old menu before sending a new one, so menus never
# stack up when triggered by a text command (e.g. /start, /admin) rather
# than a button (buttons can edit in place instead).
#
# This is a separate table (not new columns on User) so it can be created
# with a plain create_tables.py run — no ALTER TABLE needed on your
# existing users table.

from sqlalchemy import (
    Column,
    BigInteger
)

from database import Base


class MenuState(Base):
    __tablename__ = "menu_state"

    telegram_id = Column(
        BigInteger,
        primary_key=True
    )

    chat_id = Column(
        BigInteger,
        nullable=False
    )

    message_id = Column(
        BigInteger,
        nullable=False
    )
