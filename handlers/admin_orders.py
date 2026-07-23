print("✅ NEW ADMIN ORDERS FILE LOADED")

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from config import ADMIN_IDS
from database import SessionLocal
from models.order import Order
from models.user import User

router = Router()


def is_admin(user_id: int):
    return user_id in ADMIN_IDS


# ==========================================================
# ADMIN ORDERS
# ==========================================================

@router.callback_query(
    F.data == "admin_orders"
)
async def admin_orders(
        callback: CallbackQuery
):

    if not is_admin(
            callback.from_user.id
    ):
        await callback.answer(
            "Access denied.",
            show_alert=True
        )
        return

    db = SessionLocal()

    try:

        orders = (
            db.query(Order)
            .order_by(
                Order.id.desc()
            )
            .limit(50)
            .all()
        )

        if not orders:

            await callback.message.edit_text(
                "❌ No orders found.",
                reply_markup=
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅ Back",
                                callback_data="admin_panel"
                            )
                        ]
                    ]
                )
            )

            await callback.answer()
            return

        keyboard = []

        for order in orders:

            if order.refunded:
                icon = "💸"

            elif order.status == "completed":
                icon = "✅"

            else:
                icon = "⏳"

            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=
                        f"#{order.id} "
                        f"{icon} "
                        f"{order.product_name}",

                        callback_data=
                        f"order_{order.id}"
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
            "📦 Latest Orders",
            reply_markup=
            InlineKeyboardMarkup(
                inline_keyboard=keyboard
            )
        )

        await callback.answer()

    finally:

        db.close()


# ==========================================================
# SINGLE ORDER
# ==========================================================

@router.callback_query(
    F.data.startswith(
        "order_"
    )
)
async def order_info(
        callback: CallbackQuery
):

    order_id = int(
        callback.data.split("_")[1]
    )

    db = SessionLocal()

    try:

        order = (
            db.query(Order)
            .filter(
                Order.id == order_id
            )
            .first()
        )

        if not order:

            await callback.answer(
                "Order not found."
            )
            return

        delivered = (
            order.delivered_account
            or "N/A"
        )

        text = f"""
📦 Order #{order.id}

👤 User ID:
<code>{order.telegram_id}</code>

📦 Product:
{order.product_name}

🔑 Delivered:
<code>{delivered}</code>

💰 Amount:
${float(order.amount):.2f}

📄 Status:
{order.status}

📅 Date:
{order.created_at}

Refunded:
{"✅ YES" if order.refunded else "❌ NO"}
"""

        keyboard = []

        if not order.refunded:

            keyboard.append(
                [
                    InlineKeyboardButton(
                        text="💸 Refund",
                        callback_data=
                        f"refund_{order.id}"
                    )
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton(
                    text="🗑 Delete",
                    callback_data=
                    f"delete_order_{order.id}"
                )
            ]
        )

        keyboard.append(
            [
                InlineKeyboardButton(
                    text="⬅ Back",
                    callback_data="admin_orders"
                )
            ]
        )

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=
            InlineKeyboardMarkup(
                inline_keyboard=keyboard
            )
        )

        await callback.answer()

    finally:

        db.close()


# ==========================================================
# REFUND
# ==========================================================

@router.callback_query(
    F.data.startswith(
        "refund_"
    )
)
async def refund_order(
        callback: CallbackQuery
):

    order_id = int(
        callback.data.split("_")[1]
    )

    db = SessionLocal()

    try:

        order = (
            db.query(Order)
            .filter(
                Order.id == order_id
            )
            .first()
        )

        if not order:

            await callback.answer(
                "Order not found."
            )
            return

        if order.refunded:

            await callback.answer(
                "Already refunded."
            )
            return

        user = (
            db.query(User)
            .filter(
                User.telegram_id ==
                order.telegram_id
            )
            .first()
        )

        if not user:

            await callback.answer(
                "User not found."
            )
            return

        user.balance += float(
            order.amount
        )

        order.refunded = True
        order.status = "refunded"

        db.commit()

        await callback.answer(
            "✅ Refunded"
        )

        callback.data = (
            f"order_{order.id}"
        )

        await order_info(
            callback
        )

    finally:

        db.close()


# ==========================================================
# DELETE
# ==========================================================

@router.callback_query(
    F.data.startswith(
        "delete_order_"
    )
)
async def delete_order(
        callback: CallbackQuery
):

    order_id = int(
        callback.data.split("_")[2]
    )

    db = SessionLocal()

    try:

        order = (
            db.query(Order)
            .filter(
                Order.id == order_id
            )
            .first()
        )

        if not order:

            await callback.answer(
                "Order not found."
            )
            return

        db.delete(order)

        db.commit()

    finally:

        db.close()

    await callback.message.edit_text(
        "✅ Order deleted.",
        reply_markup=
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📦 Orders",
                        callback_data=
                        "admin_orders"
                    )
                ]
            ]
        )
    )

    await callback.answer()