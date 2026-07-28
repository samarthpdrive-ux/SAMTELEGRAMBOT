import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext

from sqlalchemy.exc import SQLAlchemyError

import config
from config import ADMIN_IDS
from database import SessionLocal, transaction, retry_on_write_conflict
from models.order import Order
from models.user import User
from models.product import Product
from states.delivery_states import DeliverOrder

from handlers.products import (
    REFERRAL_COMMISSION_RATE,
    REFERRAL_CREDIT_TO_BALANCE,
    _money,
)

logger = logging.getLogger(__name__)

router = Router()

STATUS_LABELS = {
    "completed": "✅ Completed",
    "pending_manual": "⏳ Pending (manual fulfillment)",
    "preorder": "📦 Preorder (waitlisted)",
    "refunded": "💸 Refunded",
    "deleted": "🗑 Deleted",
}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ==========================================================
# ADMIN ORDERS LIST
# ==========================================================

@router.callback_query(F.data == "admin_orders")
async def admin_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    db = SessionLocal()
    try:
        # Soft-deleted orders never show up in the admin list again —
        # see delete_order() below, which sets status="deleted"
        # instead of removing the row.
        orders = (
            db.query(Order)
            .filter(Order.status != "deleted")
            .order_by(Order.id.desc())
            .limit(50)
            .all()
        )

        if not orders:
            await callback.message.edit_text(
                "❌ No orders found.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="⬅ Back", callback_data="admin_panel")]
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

            keyboard.append([
                InlineKeyboardButton(
                    text=f"#{order.id} {icon} {order.product_name}",
                    callback_data=f"order_{order.id}"
                )
            ])

        keyboard.append([InlineKeyboardButton(text="⬅ Back", callback_data="admin_panel")])

        await callback.message.edit_text(
            "📦 Latest Orders",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        await callback.answer()

    finally:
        db.close()


# ==========================================================
# SINGLE ORDER
# ==========================================================

@router.callback_query(F.data.startswith("order_"))
async def order_info(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])

    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()

        if not order:
            await callback.answer("Order not found.")
            return

        delivered = order.delivered_account or "Not delivered yet"
        status_label = STATUS_LABELS.get(order.status, order.status)

        text = f"""
📦 Order #{order.id}

👤 User ID:
<code>{order.telegram_id}</code>

📦 Product:
{order.product_name} x{order.quantity or 1}

🔑 Delivered:
<code>{delivered}</code>

💰 Amount:
${_money(order.amount):.2f}

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
            keyboard.append([
                InlineKeyboardButton(text="📤 Deliver", callback_data=f"deliver_order_{order.id}")
            ])

        if not order.refunded and order.status != "deleted":
            keyboard.append([
                InlineKeyboardButton(text="💸 Refund", callback_data=f"refund_{order.id}")
            ])

        if order.status != "deleted":
            keyboard.append([
                InlineKeyboardButton(text="🗑 Delete", callback_data=f"delete_order_{order.id}")
            ])

        keyboard.append([InlineKeyboardButton(text="⬅ Back", callback_data="admin_orders")])

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        await callback.answer()

    finally:
        db.close()


# ==========================================================
# REFUND — restores stock, reverses any referral commission
# already paid on this order, all in one transaction
# ==========================================================

@retry_on_write_conflict(max_attempts=3)
def _do_refund(order_id: int, admin_id: int) -> dict:
    with transaction() as db:
        order = (
            db.query(Order)
            .filter(Order.id == order_id)
            .with_for_update()
            .first()
        )

        if not order:
            return {"error": "Order not found."}
        if order.refunded:
            return {"error": "Already refunded."}
        if order.status == "deleted":
            return {"error": "This order was deleted and can no longer be refunded."}

        buyer = (
            db.query(User)
            .filter(User.telegram_id == order.telegram_id)
            .with_for_update()
            .first()
        )
        if not buyer:
            return {"error": "Buyer account not found."}

        refund_amount = _money(order.amount)

        # --- Restore stock -------------------------------------
        # Only automatically-delivered accounts can be meaningfully
        # "put back" (as text, appended back onto file_content) — a
        # manual-fulfillment unit that was hand-delivered has no
        # generic representation to restore, so for "manual"/hybrid
        # orders without a delivered_account we restore the numeric
        # stock counter instead. Preorders and never-fulfilled manual
        # orders never decremented stock in the first place, so there
        # is nothing to restore for those.
        product = (
            db.query(Product)
            .filter(Product.id == order.product_id)
            .with_for_update()
            .first()
        )

        stock_restored = False
        if product is not None and not order.is_preorder:
            if order.delivered_account and order.status == "completed":
                # Automatic (or hybrid-auto) delivery: the exact
                # account strings were consumed from file_content —
                # put them back so they can be resold.
                restored_accounts = [
                    a.strip() for a in order.delivered_account.splitlines() if a.strip()
                ]
                if restored_accounts:
                    existing = (
                        [a.strip() for a in product.file_content.splitlines() if a.strip()]
                        if product.file_content else []
                    )
                    product.file_content = "\n".join(restored_accounts + existing)
                    product.stock = len(restored_accounts) + len(existing)
                    stock_restored = True
            elif order.status in ("pending_manual", "completed"):
                # Manual/hybrid-manual fulfillment: only the numeric
                # counter moved, so give the units back there.
                product.stock = (product.stock or 0) + (order.quantity or 1)
                stock_restored = True

        # --- Reverse referral commission, if one was paid ------
        # Commission is paid at purchase time for completed /
        # pending_manual orders (see handlers/products.py), or at
        # delivery time for a fulfilled preorder (see
        # deliver_order_finish below). Either way, by the time an
        # order can be refunded it is one of: completed,
        # pending_manual, or preorder-that-was-never-delivered (no
        # commission was ever paid on that last one).
        commission_reversed = None
        if order.status != "preorder" and buyer.referred_by:
            referrer = (
                db.query(User)
                .filter(User.telegram_id == buyer.referred_by)
                .with_for_update()
                .first()
            )
            if referrer is not None:
                commission = _money(refund_amount * REFERRAL_COMMISSION_RATE)
                if commission > 0:
                    referrer.referral_earnings = max(
                        Decimal("0"),
                        _money(referrer.referral_earnings) - commission,
                    )
                    if REFERRAL_CREDIT_TO_BALANCE:
                        referrer.balance = _money(referrer.balance) - commission
                    commission_reversed = {
                        "referrer_telegram_id": referrer.telegram_id,
                        "amount": commission,
                    }

        # --- Refund the buyer -----------------------------------
        buyer.balance = _money(buyer.balance) + refund_amount

        order.refunded = True
        order.status = "refunded"

        logger.info(
            "Order %s refunded by admin %s | buyer=%s amount=%s stock_restored=%s",
            order.id, admin_id, order.telegram_id, refund_amount, stock_restored,
        )

        return {
            "order_id": order.id,
            "buyer_id": order.telegram_id,
            "amount": refund_amount,
            "stock_restored": stock_restored,
            "commission_reversed": commission_reversed,
        }


@router.callback_query(F.data.startswith("refund_"))
async def refund_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[1])

    try:
        result = await _dispatch_blocking(_do_refund, order_id, callback.from_user.id)
    except SQLAlchemyError:
        logger.exception("Database error refunding order %s", order_id)
        await callback.answer("❌ Database error while refunding. Please try again.", show_alert=True)
        return
    except Exception:
        logger.exception("Unexpected error refunding order %s", order_id)
        await callback.answer("❌ Unexpected error while refunding.", show_alert=True)
        return

    if "error" in result:
        await callback.answer(result["error"], show_alert=True)
        return

    await callback.answer("✅ Refunded")

    try:
        await callback.bot.send_message(
            result["buyer_id"],
            "💸 Your order was refunded.\n\n"
            f"💰 Amount returned to your balance: ${result['amount']:.2f}"
        )
    except Exception:
        logger.exception("Failed to notify buyer %s of refund", result["buyer_id"])

    callback.data = f"order_{order_id}"
    await order_info(callback)


# ==========================================================
# DELETE — soft delete only, never removes the row
# ==========================================================

@router.callback_query(F.data.startswith("delete_order_"))
async def delete_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[2])

    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()

        if not order:
            await callback.answer("Order not found.")
            return

        # Soft delete: keep the row (audit trail, refund history,
        # referral-commission bookkeeping) but hide it from the admin
        # orders list and stop offering further actions on it.
        order.status = "deleted"
        db.commit()

        logger.info("Order %s soft-deleted by admin %s", order_id, callback.from_user.id)

    finally:
        db.close()

    await callback.message.edit_text(
        "✅ Order deleted.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📦 Orders", callback_data="admin_orders")]
            ]
        )
    )
    await callback.answer()


# ==========================================================
# MANUAL DELIVERY (for "manual"/"hybrid" delivery type orders
# and preorders — admin sends the account/key by hand)
# ==========================================================

@router.callback_query(F.data.startswith("deliver_order_"))
async def deliver_order_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[2])

    await state.update_data(deliver_order_id=order_id)
    await state.set_state(DeliverOrder.content)

    await callback.message.answer(
        "📤 Send the content to deliver to the buyer "
        "(account, key, or any message).\n\n"
        "It will be sent to them exactly as you type it, "
        "and the order will be marked completed."
    )
    await callback.answer()


@retry_on_write_conflict(max_attempts=3)
def _do_deliver(order_id: int, delivered_text: str) -> dict:
    with transaction() as db:
        order = (
            db.query(Order)
            .filter(Order.id == order_id)
            .with_for_update()
            .first()
        )

        if not order:
            return {"error": "Order not found."}
        if order.refunded:
            return {"error": "This order was already refunded, not delivering it."}
        if order.status == "deleted":
            return {"error": "This order was deleted."}

        was_preorder = order.is_preorder

        order.delivered_account = delivered_text
        order.status = "completed"
        order.is_preorder = False

        # Referral commission for a preorder is deliberately deferred
        # until it's actually fulfilled (see handlers/products.py) —
        # pay it now, exactly once, on that transition. A
        # non-preorder pending_manual order already had its
        # commission paid at purchase time, so it must NOT be paid
        # again here.
        commission_paid = None
        if was_preorder:
            buyer = (
                db.query(User)
                .filter(User.telegram_id == order.telegram_id)
                .with_for_update()
                .first()
            )
            if buyer and buyer.referred_by:
                referrer = (
                    db.query(User)
                    .filter(User.telegram_id == buyer.referred_by)
                    .with_for_update()
                    .first()
                )
                if referrer is not None:
                    commission = _money(_money(order.amount) * REFERRAL_COMMISSION_RATE)
                    if commission > 0:
                        referrer.referral_earnings = _money(referrer.referral_earnings) + commission
                        if REFERRAL_CREDIT_TO_BALANCE:
                            referrer.balance = _money(referrer.balance) + commission
                        commission_paid = {
                            "referrer_telegram_id": referrer.telegram_id,
                            "amount": commission,
                        }

        logger.info("Order %s delivered manually (was_preorder=%s)", order.id, was_preorder)

        return {
            "order_id": order.id,
            "buyer_id": order.telegram_id,
            "product_name": order.product_name,
            "delivered_text": delivered_text,
            "commission_paid": commission_paid,
        }


async def _dispatch_blocking(func, *args):
    """Small shared helper so both this module's handlers and
    products.py's use the same off-event-loop dispatch pattern."""
    return await asyncio.to_thread(func, *args)


@router.message(DeliverOrder.content)
async def deliver_order_finish(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    order_id = data.get("deliver_order_id")
    await state.clear()

    if not order_id:
        return

    try:
        result = await _dispatch_blocking(_do_deliver, order_id, message.text)
    except SQLAlchemyError:
        logger.exception("Database error delivering order %s", order_id)
        await message.answer("❌ Database error while delivering. Please try again.")
        return
    except Exception:
        logger.exception("Unexpected error delivering order %s", order_id)
        await message.answer("❌ Unexpected error while delivering.")
        return

    if "error" in result:
        await message.answer(f"❌ {result['error']}")
        return

    try:
        await message.bot.send_message(
            result["buyer_id"],
            "✅ Your order has been delivered!\n\n"
            f"📦 Product:\n{result['product_name']}\n\n"
            "🔑 Details:\n\n"
            f"<code>{result['delivered_text']}</code>",
            parse_mode="HTML"
        )
    except Exception:
        logger.exception("Failed to notify buyer %s of delivery", result["buyer_id"])
        await message.answer(
            "⚠️ Order marked delivered, but I couldn't message the "
            "buyer directly (they may have blocked the bot)."
        )
        return

    commission = result.get("commission_paid")
    if commission:
        try:
            await message.bot.send_message(
                commission["referrer_telegram_id"],
                "🎉 You earned a referral commission!\n\n"
                f"💵 Amount: ${commission['amount']:.2f}\n"
                "Thanks for sharing your link!"
            )
        except Exception:
            logger.exception(
                "Failed to notify referrer %s of commission",
                commission["referrer_telegram_id"],
            )

    await message.answer(
        "✅ Delivered and buyer notified.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅ Back to Orders", callback_data="admin_orders")]
            ]
        )
    )
