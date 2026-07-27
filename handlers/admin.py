from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext

from database import SessionLocal
from config import ADMIN_IDS

from models.user import User
from models.product import Product
from models.order import Order
from models.deposit import Deposit
from models.ticket import Ticket

from keyboards.admin_menu import get_admin_panel
from keyboards.menu import get_admin_main_menu

from states.broadcast import BroadcastState

router = Router()


# =====================================================
# /admin
# =====================================================

@router.message(Command("admin"))
async def admin_cmd(
        message: Message
):

    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer(
        "👑 Admin Panel",
        reply_markup=get_admin_panel()
    )


# =====================================================
# ADMIN PANEL
# =====================================================

@router.callback_query(
    F.data == "admin_panel"
)
async def admin_panel(
        callback: CallbackQuery
):

    if callback.from_user.id not in ADMIN_IDS:

        await callback.answer(
            "Access denied.",
            show_alert=True
        )

        return

    await callback.message.edit_text(
        "👑 Admin Panel",
        reply_markup=get_admin_panel()
    )

    await callback.answer()




# =====================================================
# USERS
# =====================================================

@router.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    db=SessionLocal()
    try:
        users=db.query(User).order_by(User.id.desc()).all()
        keyboard=[[InlineKeyboardButton(text=f"{u.full_name} | {u.telegram_id}",callback_data=f"view_user_{u.id}")] for u in users]
        keyboard.append([InlineKeyboardButton(text="⬅ Back",callback_data="admin_panel")])
        await callback.message.edit_text(
            f"👥 Users ({len(users)})",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
    finally:
        db.close()
    await callback.answer()

@router.callback_query(F.data.startswith("view_user_"))
async def view_user(callback: CallbackQuery):
    uid=int(callback.data.split("_")[2])
    db=SessionLocal()
    try:
        user=db.query(User).filter(User.id==uid).first()
        if not user:
            await callback.answer("User not found.",show_alert=True)
            return
        await callback.message.edit_text(
f'''👤 User Details

Name: {user.full_name}
Username: @{user.username or "None"}

Telegram ID:
<code>{user.telegram_id}</code>

💰 Balance:
${float(user.balance):.2f}

🎁 Referrals:
{user.total_referrals}

💵 Referral Earnings:
${float(user.referral_earnings):.2f}

🛒 Orders:
{user.total_orders}

💸 Spent:
${float(user.total_spent):.2f}

💳 Deposited:
${float(user.total_deposited):.2f}

🚫 Banned:
{"Yes" if user.is_banned else "No"}''',
parse_mode="HTML",
reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Back",callback_data="admin_users")]])
        )
    finally:
        db.close()
    await callback.answer()


# =====================================================
# PRODUCTS
# =====================================================

@router.callback_query(
    F.data == "admin_products"
)
async def products(
        callback: CallbackQuery
):

    db = SessionLocal()

    try:

        products = (
            db.query(Product)
            .order_by(Product.id.desc())
            .all()
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    text="➕ New Product",
                    callback_data="create_product"
                )
            ]
        ]

        for product in products:

            status = (
                "🟢"
                if product.is_active
                else "🔴"
            )

            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=
                        f"#{product.id} "
                        f"{status} "
                        f"{product.icon} "
                        f"{product.name} "
                        f"({product.stock})",

                        callback_data=
                        f"manage_{product.id}"
                    )
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton(
                    text="⬅ Back",
                    callback_data="admin_panel"
                )
            ]
        )

        await callback.message.edit_text(
            "📦 Product Management",
            reply_markup=
            InlineKeyboardMarkup(
                inline_keyboard=keyboard
            )
        )

    finally:
        db.close()

    await callback.answer()


# =====================================================
# STATISTICS
# =====================================================

@router.callback_query(
    F.data == "admin_stats"
)
async def admin_stats(
        callback: CallbackQuery
):

    db = SessionLocal()

    try:

        users = db.query(User).count()

        products = db.query(Product).count()

        orders = db.query(Order).count()

        deposits = db.query(Deposit).count()

        tickets = db.query(Ticket).count()

        revenue = sum(
            float(x.amount)
            for x in db.query(Order).all()
        )

        text = f"""
📊 Statistics

👥 Users: {users}
📦 Products: {products}
🛒 Orders: {orders}
💰 Deposits: {deposits}
🎫 Tickets: {tickets}

💵 Revenue:
${revenue:.2f}
"""

        await callback.message.edit_text(
            text,
            reply_markup=
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅ Back",
                            callback_data=
                            "admin_panel"
                        )
                    ]
                ]
            )
        )

    finally:
        db.close()

    await callback.answer()


# =====================================================
# BROADCAST START
# =====================================================

@router.callback_query(
    F.data == "admin_broadcast"
)
async def broadcast_start(
        callback: CallbackQuery,
        state: FSMContext
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    await state.set_state(
        BroadcastState.waiting_message
    )

    await callback.message.answer(
        "📢 Send message to broadcast."
    )

    await callback.answer()


# =====================================================
# SEND BROADCAST
# =====================================================

@router.message(
    BroadcastState.waiting_message
)
async def send_broadcast(
        message: Message,
        state: FSMContext
):

    if message.from_user.id not in ADMIN_IDS:
        return

    db = SessionLocal()

    try:

        users = (
            db.query(User.telegram_id)
            .all()
        )

        user_ids = [
            x[0]
            for x in users
        ]

    finally:
        db.close()

    sent = 0
    failed = 0

    status = await message.answer(
        "📢 Broadcasting..."
    )

    for user_id in user_ids:

        try:

            await message.copy_to(
                chat_id=user_id
            )

            sent += 1

        except:

            failed += 1

    await status.edit_text(
        f"""
📢 Broadcast Finished

✅ Sent: {sent}
❌ Failed: {failed}
"""
    )

    await state.clear()


# =====================================================
# BACK TO USER PANEL
# =====================================================

@router.callback_query(
    F.data == "admin_back"
)
async def admin_back(
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

        text = (
            f"👤 {user.full_name}\n"
            f"🆔 ID: {user.telegram_id}\n"
            f"💰 Balance: "
            f"${float(user.balance):.2f}\n"
            f"🎁 Referral Earnings: "
            f"${float(user.referral_earnings):.2f}\n\n"
            f"👇 Choose an option:"
        )

        await callback.message.edit_text(
            text,
            reply_markup=
            get_admin_main_menu()
        )

    finally:
        db.close()

    await callback.answer()
