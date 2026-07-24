import asyncio

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext

from database import SessionLocal
from models.ticket import Ticket
from config import ADMIN_IDS
from states.support import SupportState

router = Router()

CONTACT_USERNAME = "sudarshan_ch"


# =====================================================
# CONTACT
# =====================================================

@router.callback_query(F.data == "contact_info")
async def contact(callback: CallbackQuery):
    # Ack Telegram first — no reason to make the callback token's
    # validity depend on whether message.answer() succeeds quickly.
    await callback.answer()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📞 Open Chat",
                    url=f"https://t.me/{CONTACT_USERNAME}"
                )
            ]
        ]
    )

    await callback.message.answer(
        "📞 Contact Admin",
        reply_markup=markup
    )


# =====================================================
# SUPPORT MENU
# =====================================================

@router.callback_query(F.data == "support_ticket")
async def support(
        callback: CallbackQuery,
        state: FSMContext
):
    await callback.answer()

    await state.set_state(SupportState.waiting_message)

    await callback.message.answer(
        "🎫 Send your issue.\n\n"
        "Your next message will create a ticket."
    )


# =====================================================
# CREATE TICKET
# =====================================================

def _save_ticket(user_id: int, text: str) -> int:
    """Blocking DB write, run in a thread."""
    db = SessionLocal()
    try:
        ticket = Ticket(
            user_id=user_id,
            message=text,
            status="Open"
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)
        return ticket.id
    finally:
        db.close()


@router.message(SupportState.waiting_message)
async def create_ticket(
        message: Message,
        state: FSMContext
):
    ticket_id = await asyncio.to_thread(
        _save_ticket,
        message.from_user.id,
        message.text,
    )

    await state.clear()

    await message.answer(
        f"✅ Ticket Created\n\n"
        f"Ticket ID: {ticket_id}\n\n"
        f"Support will reply soon."
    )

    # Notify admins
    for admin in ADMIN_IDS:
        try:
            username = (
                f"@{message.from_user.username}"
                if message.from_user.username
                else "No username"
            )

            await message.bot.send_message(
                admin,
                f"🎫 New Ticket\n\n"
                f"Ticket ID: {ticket_id}\n\n"
                f"User ID: {message.from_user.id}\n"
                f"Username: {username}\n\n"
                f"Message:\n\n"
                f"{message.text}"
            )

        except Exception as e:
            print("Admin ticket error:", e)
