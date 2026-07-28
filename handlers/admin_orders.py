print("✅ NEW ADMIN ORDERS FILE LOADED")

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext

from config import ADMIN_IDS
from database import SessionLocal
from models.order import Order
from models.user import User
from states.delivery_states import DeliverOrder

router = Router()

STATUS_LABELS = {
    "completed": "✅ Completed",
    "pending_manual": "⏳ Pending (manual fulfillment)",
    "preorder": "📦 Preorder (waitlisted)",
    "refunded": "💸 Refunded",
}


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

            elif order.status == "preorder":
                icon = "📦"

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
            or "Not delivered yet"
        )

        status_label = STATUS_LABELS.get(
            order.status,
            order.status
        )

        text = f"""
📦 Order #{order.id}

👤 User ID:
<code>{order.telegram_id}</code>

📦 Product:
{order.product_name} x{order.quantity or 1}

🔑 Delivered:
<code>{delivered}</code>

💰 Amount:
${float(order.amount):.2f}

📄 Status:
{status_label}

📅 Date:
{order.created_at}

Refunded:
{"✅ YES" if order.refunded else "❌ NO"}
"""

        keyboard = []

        needs_fulfillment = (
            order.status in ("pending_manual", "preorder")
            and not order.refunded
        )

        if needs_fulfillment:

            keyboard.append(
                [
                    InlineKeyboardButton(
                        text="📤 Deliver",
                        callback_data=
                        f"deliver_order_{order.id}"
                    )
                ]
            )

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


# ==========================================================
# MANUAL DELIVERY (for "manual"/"hybrid" delivery type orders
# and preorders — admin sends the account/key by hand)
# ==========================================================

@router.callback_query(
    F.data.startswith(
        "deliver_order_"
    )
)
async def deliver_order_start(
        callback: CallbackQuery,
        state: FSMContext
):

    if not is_admin(
            callback.from_user.id
    ):
        await callback.answer(
            "Access denied.",
            show_alert=True
        )
        return

    order_id = int(
        callback.data.split("_")[2]
    )

    await state.update_data(
        deliver_order_id=order_id
    )

    await state.set_state(
        DeliverOrder.content
    )

    await callback.message.answer(
        "📤 Send the content to deliver to the buyer "
        "(account, key, or any message).\n\n"
        "It will be sent to them exactly as you type it, "
        "and the order will be marked completed."
    )

    await callback.answer()


@router.message(
    DeliverOrder.content
)
async def deliver_order_finish(
        message: Message,
        state: FSMContext
):

    if not is_admin(
            message.from_user.id
    ):
        return

    data = await state.get_data()

    order_id = data.get(
        "deliver_order_id"
    )

    await state.clear()

    if not order_id:
        return

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

            await message.answer(
                "❌ Order not found."
            )
            return

        if order.refunded:

            await message.answer(
                "❌ This order was already refunded, "
                "not delivering it."
            )
            return

        order.delivered_account = message.text
        order.status = "completed"
        order.is_preorder = False

        db.commit()

        buyer_id = order.telegram_id
        product_name = order.product_name
        delivered_text = message.text

    finally:
        db.close()

    try:

        await message.bot.send_message(
            buyer_id,
            "✅ Your order has been delivered!\n\n"
            f"📦 Product:\n{product_name}\n\n"
            "🔑 Details:\n\n"
            f"<code>{delivered_text}</code>",
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            "DELIVER NOTIFY ERROR:",
            e
        )

        await message.answer(
            "⚠️ Order marked delivered, but I couldn't message the "
            "buyer directly (they may have blocked the bot)."
        )
        return

    await message.answer(
        "✅ Delivered and buyer notified.",
        reply_markup=
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅ Back to Orders",
                        callback_data=
                        "admin_orders"
                    )
                ]
            ]
        )
    )
