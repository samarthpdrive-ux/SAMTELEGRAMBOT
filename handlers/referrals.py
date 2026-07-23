from aiogram import Router, F
from aiogram.types import CallbackQuery

from database import SessionLocal
from models.user import User

router = Router()


# =====================================================
# REFERRALS
# =====================================================

@router.callback_query(
    F.data == "referrals_menu"
)
async def referrals(
        callback: CallbackQuery
):

    db = SessionLocal()

    try:

        user = (
            db.query(User)
            .filter(
                User.telegram_id ==
                callback.from_user.id
            )
            .first()
        )

        if not user:

            await callback.answer(
                "User not found.",
                show_alert=True
            )
            return

        bot = await callback.bot.get_me()

        text = (
            f"👥 Total Referrals: "
            f"{user.total_referrals}\n\n"
            f"💵 Earnings: "
            f"${float(user.referral_earnings):.2f}\n\n"
            f"🔗 Referral Link:\n"
            f"https://t.me/"
            f"{bot.username}"
            f"?start="
            f"{user.referral_code}"
        )

        await callback.message.answer(
            text
        )

    finally:

        db.close()

    await callback.answer()