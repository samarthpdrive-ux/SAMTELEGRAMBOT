import asyncio
import logging
import uuid

from aiogram import Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, CallbackQuery

from database import SessionLocal, transaction, retry_on_write_conflict
from models.user import User
from config import ADMIN_IDS

from keyboards.menu import get_main_menu, get_admin_main_menu

logger = logging.getLogger(__name__)

router = Router()


def generate_ref_code() -> str:
    return str(uuid.uuid4())[:8].upper()


async def send_dashboard(target, telegram_id: int):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            return

        text = (
            f"👤 {user.full_name}\n"
            f"🆔 ID: {telegram_id}\n"
            f"💰 Balance: ${user.balance_display:.2f}\n"
            f"🎁 Referral Earnings: ${user.referral_earnings_display:.2f}\n\n"
            f"👇 Choose an option:"
        )

        keyboard = get_admin_main_menu() if telegram_id in ADMIN_IDS else get_main_menu()

        if isinstance(target, Message):
            await target.answer(text, reply_markup=keyboard)
        else:
            await target.message.edit_text(text, reply_markup=keyboard)

    finally:
        db.close()


@retry_on_write_conflict(max_attempts=3)
def _get_or_create_user(telegram_id: int, username: str, full_name: str, ref_payload: str) -> None:
    """
    Creates the user row if it doesn't exist yet (attributing them to
    a referrer if a valid /start payload was supplied and this is
    truly their first time), and keeps username/full_name in sync on
    every /start either way. All in one transaction so a signup can
    never end up half-written.
    """
    with transaction() as db:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()

        if not user:
            referred_by = None

            # Only ever attribute a NEW user, and only to a real,
            # different referrer — never let someone refer themselves
            # by sharing their own link.
            if ref_payload:
                referrer = (
                    db.query(User)
                    .filter(User.referral_code == ref_payload)
                    .with_for_update()
                    .first()
                )
                if referrer is not None and referrer.telegram_id != telegram_id:
                    referred_by = referrer.telegram_id
                    referrer.total_referrals = (referrer.total_referrals or 0) + 1

            user = User(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                balance=0,
                referral_code=generate_ref_code(),
                referred_by=referred_by,
                total_referrals=0,
                referral_earnings=0,
                total_orders=0,
                total_spent=0,
                total_deposited=0,
                is_banned=False,
            )
            db.add(user)
            db.flush()

        user.username = username
        user.full_name = full_name


@router.message(CommandStart())
async def start_cmd(message: Message, command: CommandObject):
    telegram_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name

    # /start <payload> — the payload is expected to be a referral_code
    # from another user's referral link (see handlers/referrals.py).
    ref_payload = (command.args or "").strip() if command else ""

    try:
        await asyncio.to_thread(_get_or_create_user, telegram_id, username, full_name, ref_payload)
    except Exception:
        logger.exception("Failed to create/update user %s on /start", telegram_id)
        await message.answer("❌ Something went wrong starting your session. Please try again.")
        return

    await send_dashboard(message, telegram_id)
