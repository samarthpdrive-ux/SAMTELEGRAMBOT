from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery
)

import uuid

from database import SessionLocal
from models.user import User
from config import ADMIN_IDS

from keyboards.menu import (
    get_main_menu,
    get_admin_main_menu
)

router = Router()


def generate_ref_code():
    return str(uuid.uuid4())[:8].upper()


async def send_dashboard(
        target,
        telegram_id: int
):

    db = SessionLocal()

    try:

        user = (
            db.query(User)
            .filter(
                User.telegram_id == telegram_id
            )
            .first()
        )

        if not user:
            return

        text = (
            f"👤 {user.full_name}\n"
            f"🆔 ID: {telegram_id}\n"
            f"💰 Balance: "
            f"${float(user.balance):.2f}\n"
            f"🎁 Referral Earnings: "
            f"${float(user.referral_earnings):.2f}\n\n"
            f"👇 Choose an option:"
        )

        keyboard = (
            get_admin_main_menu()
            if telegram_id in ADMIN_IDS
            else get_main_menu()
        )

        if isinstance(target, Message):

            await target.answer(
                text,
                reply_markup=keyboard
            )

        else:

            await target.message.edit_text(
                text,
                reply_markup=keyboard
            )

    finally:
        db.close()


@router.message(CommandStart())
async def start_cmd(
        message: Message
):

    db = SessionLocal()

    try:

        telegram_id = message.from_user.id
        username = message.from_user.username
        full_name = message.from_user.full_name

        user = (
            db.query(User)
            .filter(
                User.telegram_id == telegram_id
            )
            .first()
        )

        if not user:

            user = User(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                balance=0,
                referral_code=generate_ref_code(),
                total_referrals=0,
                referral_earnings=0,
                total_orders=0,
                total_spent=0,
                total_deposited=0,
                is_banned=False
            )

            db.add(user)
            db.commit()
            db.refresh(user)

        user.username = username
        user.full_name = full_name

        db.commit()

    finally:
        db.close()

    await send_dashboard(
        message,
        telegram_id
    )
