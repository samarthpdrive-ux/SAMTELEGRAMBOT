from aiogram import Router, F
from aiogram.types import (
    CallbackQuery
)

from database import SessionLocal
from models.order import Order

router = Router()


# =====================================================
# MY ORDERS
# =====================================================

@router.callback_query(
    F.data == "orders_menu"
)
async def my_orders(
        callback: CallbackQuery
):

    db = SessionLocal()

    try:

        user_id = (
            callback.from_user.id
        )

        print(
            "USER ID:",
            user_id
        )

        orders = (
            db.query(Order)
            .filter(
                Order.telegram_id
                ==
                user_id
            )
            .order_by(
                Order.id.desc()
            )
            .all()
        )

        print(
            "ORDERS FOUND:",
            len(orders)
        )

    finally:

        db.close()

    # =================================================
    # NO ORDERS
    # =================================================

    if not orders:

        await callback.message.answer(
            "📦 You have no orders."
        )

        await callback.answer()

        return

    # =================================================
    # SHOW ORDERS
    # =================================================

    text = "📦 Your Orders\n\n"

    for order in orders:

        status = (
            "✅ Completed"
            if order.status == "completed"
            else order.status
        )

        text += (
            f"🆔 Order #{order.id}\n"
            f"📦 Product: "
            f"{order.product_name}\n"
            f"💰 Amount: "
            f"${float(order.amount):.2f}\n"
            f"📄 Status: "
            f"{status}\n"
        )

        # =============================================
        # DELIVERED ACCOUNT
        # =============================================

        if getattr(
                order,
                "delivered_account",
                None
        ):

            text += (
                "🔑 Account:\n"
                f"<code>"
                f"{order.delivered_account}"
                f"</code>\n"
            )

        # =============================================
        # DATE
        # =============================================

        if getattr(
                order,
                "created_at",
                None
        ):

            text += (
                "📅 Date: "
                f"{order.created_at.strftime('%d-%m-%Y %H:%M')}\n"
            )

        text += (
            "\n━━━━━━━━━━━━━━\n\n"
        )

    await callback.message.answer(
        text,
        parse_mode="HTML"
    )

    await callback.answer()