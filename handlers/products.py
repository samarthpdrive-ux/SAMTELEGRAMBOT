import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext

from sqlalchemy.exc import SQLAlchemyError

import config
from config import ADMIN_IDS
from database import SessionLocal, transaction, retry_on_write_conflict
from models.product import Product
from models.user import User
from models.order import Order

logger = logging.getLogger(__name__)

router = Router()

# How many units a customer can order in one go when there's no real
# stock ceiling (e.g. an out-of-stock preorder). Keeps the + button from
# scrolling to infinity.
PREORDER_MAX_QTY = 10
DEFAULT_LOW_STOCK_THRESHOLD = 3

# Referral commission paid to whoever referred the buyer, taken as a
# fraction of the order total. Purely additive config — falls back to
# 5% / crediting straight to balance if not set in config.py.
try:
    REFERRAL_COMMISSION_RATE = Decimal(
        str(getattr(config, "REFERRAL_COMMISSION_RATE", "0.05"))
    )
except (InvalidOperation, ValueError):
    REFERRAL_COMMISSION_RATE = Decimal("0.05")

# If True (default), commission is added to the referrer's *usable*
# balance as well as their referral_earnings counter. If False,
# referral_earnings is updated for display/reporting only and the
# referrer's balance is untouched (e.g. if payouts are handled
# manually/off-platform).
REFERRAL_CREDIT_TO_BALANCE = getattr(config, "REFERRAL_CREDIT_TO_BALANCE", True)

MONEY_QUANT = Decimal("0.00000001")  # matches DECIMAL(20, 8)


def _money(value) -> Decimal:
    """Coerce any numeric-ish value into a DECIMAL(20, 8)-safe Decimal.
    Always go through str() first — Decimal(0.1) != Decimal("0.1")."""
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


# =====================================================
# PER-USER PURCHASE LOCK (in-process double-tap guard)
# =====================================================
#
# The DB transaction below is what keeps balance/stock CORRECT even
# under concurrency (row locks serialize concurrent purchases by the
# same user). It does NOT stop two rapid taps of "Confirm Purchase"
# from both being *accepted* as two separate, legitimate-looking
# orders before the first one's Telegram message has even updated.
# This in-memory lock exists purely to make the second tap a no-op
# instead of a second order.
_purchase_locks: dict[int, asyncio.Lock] = {}
_purchase_locks_guard = asyncio.Lock()


async def _get_purchase_lock(telegram_id: int) -> asyncio.Lock:
    async with _purchase_locks_guard:
        lock = _purchase_locks.get(telegram_id)
        if lock is None:
            lock = asyncio.Lock()
            _purchase_locks[telegram_id] = lock
        return lock


# =====================================================
# HELPERS (run in thread — keep DB calls off the event loop)
# =====================================================

def _fetch_active_products():
    db = SessionLocal()
    try:
        return (
            db.query(Product)
            .filter(Product.is_active == True)  # noqa: E712
            .order_by(Product.id.asc())
            .all()
        )
    finally:
        db.close()


def _fetch_product(product_id: int):
    db = SessionLocal()
    try:
        return db.query(Product).filter(Product.id == product_id).first()
    finally:
        db.close()


def _accounts(product) -> list[str]:
    if not product.file_content:
        return []
    return [a.strip() for a in product.file_content.splitlines() if a.strip()]


def _accounts_count(product) -> int:
    return len(_accounts(product))


def _real_stock(product) -> int:
    """Live, ground-truth availability given the product's delivery
    type. This — not product.stock alone — is what "in stock" means."""
    delivery_type = (product.delivery_type or "automatic").lower()
    accounts_available = _accounts_count(product)

    if delivery_type == "automatic":
        return accounts_available
    if delivery_type == "manual":
        return product.stock or 0
    return max(accounts_available, product.stock or 0)  # hybrid


def _get_max_qty(product) -> int:
    """The largest quantity we'll let someone select right now, given
    the product's delivery type, live stock, and whether preorder is on."""
    cap = _real_stock(product)

    if cap <= 0 and product.preorder:
        return PREORDER_MAX_QTY

    return max(cap, 0)


# =====================================================
# PRODUCTS MENU
# =====================================================

@router.callback_query(F.data == "products_menu")
async def products_menu(callback: CallbackQuery):
    # Ack Telegram FIRST — a callback_query token expires quickly, and
    # slow sync DB calls below must never delay this. You can only call
    # .answer() once, so results below use a normal chat message instead
    # of show_alert popups.
    await callback.answer()

    products = await asyncio.to_thread(_fetch_active_products)

    if not products:
        await callback.message.answer("No products available.")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                text=f"{p.icon or '📦'} {p.name}",
                callback_data=f"product_{p.id}"
            )
        ]
        for p in products
    ]

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await callback.message.answer(
        "🛍 Available Products:",
        reply_markup=markup
    )


# =====================================================
# PRODUCT DETAILS
# =====================================================

@router.callback_query(F.data.startswith("product_"))
async def product_info(callback: CallbackQuery):
    await callback.answer()

    product_id = int(callback.data.split("_")[1])
    product = await asyncio.to_thread(_fetch_product, product_id)

    if not product:
        await callback.message.answer("Product not found.")
        return

    max_qty = _get_max_qty(product)
    real_stock_available = _real_stock(product) > 0

    text = (
        f"{product.icon or '📦'} {product.name}\n\n"
        f"📝 Description:\n{product.description or 'No description'}\n\n"
        f"💰 Price: ${_money(product.price):.2f}\n"
        f"📦 Stock: {product.stock}\n"
        f"🏷 Category: {product.category or 'N/A'}"
    )

    if not real_stock_available and max_qty > 0:
        text += "\n\n📦 Currently out of stock — order now and it'll be delivered as a preorder."

    if max_qty <= 0:
        markup = None
        text += "\n\n❌ Out of stock."
    else:
        button_text = "🛒 Buy" if real_stock_available else "📦 Preorder"
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=button_text, callback_data=f"select_qty_{product.id}")]
            ]
        )

    await callback.message.answer(text, reply_markup=markup)


# =====================================================
# QUANTITY SELECTOR
# =====================================================

def _qty_text(product, qty: int, real_stock_available: bool) -> str:
    total = _money(product.price) * qty
    header = "📦 Preorder" if not real_stock_available else "🛒 Buy"
    return (
        f"{header}: {product.icon or '📦'} {product.name}\n\n"
        f"Quantity: {qty}\n"
        f"Total: ${total:.2f}"
    )


def _qty_keyboard(product_id: int, qty: int, max_qty: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➖", callback_data=f"qty_dec_{product_id}"),
                InlineKeyboardButton(text=str(qty), callback_data="noop"),
                InlineKeyboardButton(text="➕", callback_data=f"qty_inc_{product_id}"),
            ],
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data=f"confirm_buy_{product_id}"),
            ],
            [
                InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_buy_{product_id}"),
            ],
        ]
    )


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("select_qty_"))
async def select_qty(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    product_id = int(callback.data.split("_")[2])
    product = await asyncio.to_thread(_fetch_product, product_id)

    if not product or not product.is_active:
        await callback.message.answer("Product unavailable.")
        return

    max_qty = _get_max_qty(product)

    if max_qty <= 0:
        await callback.message.answer("❌ Out of stock.")
        return

    await state.update_data(**{f"qty_{product_id}": 1})

    real_stock_available = _real_stock(product) > 0

    await callback.message.answer(
        _qty_text(product, 1, real_stock_available),
        reply_markup=_qty_keyboard(product_id, 1, max_qty)
    )


@router.callback_query(F.data.startswith("qty_inc_") | F.data.startswith("qty_dec_"))
async def qty_adjust(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    parts = callback.data.split("_")
    direction = parts[1]
    product_id = int(parts[2])

    product = await asyncio.to_thread(_fetch_product, product_id)
    if not product:
        await callback.message.edit_text("Product not found.")
        return

    max_qty = _get_max_qty(product)
    if max_qty <= 0:
        await callback.message.edit_text("❌ Out of stock.")
        return

    data = await state.get_data()
    qty = data.get(f"qty_{product_id}", 1)
    qty += 1 if direction == "inc" else -1
    qty = max(1, min(qty, max_qty))

    await state.update_data(**{f"qty_{product_id}": qty})

    real_stock_available = _real_stock(product) > 0

    await callback.message.edit_text(
        _qty_text(product, qty, real_stock_available),
        reply_markup=_qty_keyboard(product_id, qty, max_qty)
    )


@router.callback_query(F.data.startswith("cancel_buy_"))
async def cancel_buy(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split("_")[2])
    await state.update_data(**{f"qty_{product_id}": 1})
    await callback.message.edit_text("❌ Cancelled.")
    await callback.answer()


# =====================================================
# PURCHASE — single atomic transaction
# =====================================================
#
# Everything in here (balance deduction, stock deduction, order
# creation, referral commission) either all lands, or none of it
# does. `transaction()` commits once at the very end and rolls back
# entirely on any exception; `with_for_update()` takes real row locks
# because database.py forces tidb_txn_mode=pessimistic on connect.
# `@retry_on_write_conflict` re-runs the whole attempt from scratch if
# TiDB reports a write conflict or deadlock — never a partial retry.

@retry_on_write_conflict(max_attempts=3)
def _do_purchase(telegram_id: int, product_id: int, quantity: int) -> dict:
    with transaction() as db:
        # Lock order is fixed across every call (buyer -> product ->
        # referrer) so two concurrent purchases can only ever contend
        # for the same lock in the same order, not deadlock against
        # each other. (A genuine buyer<->referrer<->buyer cycle across
        # two DIFFERENT purchases can still deadlock — TiDB detects
        # that as error 1213, which retry_on_write_conflict retries.)
        user = (
            db.query(User)
            .filter(User.telegram_id == telegram_id)
            .with_for_update()
            .first()
        )

        if not user:
            return {"error": "User not found."}

        if getattr(user, "is_banned", False):
            return {"error": "Your account is banned from making purchases."}

        product = (
            db.query(Product)
            .filter(Product.id == product_id)
            .with_for_update()
            .first()
        )

        if not product:
            return {"error": "Product not found."}
        if not product.is_active:
            return {"error": "Product unavailable."}
        if quantity < 1:
            return {"error": "Quantity must be at least 1."}

        price = _money(product.price)
        total_amount = _money(price * quantity)
        user_balance = _money(user.balance)

        if user_balance < total_amount:
            return {
                "error": f"Insufficient balance. This order costs ${total_amount:.2f}."
            }

        delivery_type = (product.delivery_type or "automatic").lower()
        threshold = (
            product.low_stock_threshold
            if product.low_stock_threshold is not None
            else DEFAULT_LOW_STOCK_THRESHOLD
        )

        accounts = _accounts(product)
        available = len(accounts)
        stock_before = product.stock or 0

        delivered_accounts: list[str] = []
        is_preorder_order = False
        new_stock = stock_before

        can_auto_deliver = delivery_type in ("automatic", "hybrid") and available >= quantity
        can_manual_fulfill = delivery_type in ("manual", "hybrid") and stock_before >= quantity

        if can_auto_deliver:
            delivered_accounts = accounts[:quantity]
            product.file_content = "\n".join(accounts[quantity:])
            new_stock = len(accounts) - quantity
            product.stock = new_stock

        elif can_manual_fulfill:
            new_stock = stock_before - quantity
            product.stock = new_stock

        elif product.preorder:
            is_preorder_order = True

        else:
            shortfall = available if delivery_type == "automatic" else stock_before
            return {"error": f"Only {shortfall} left in stock."}

        # Never let a bug above sell into negative stock.
        if new_stock < 0:
            return {"error": "Stock changed while processing your order. Please try again."}

        status = (
            "completed" if delivered_accounts
            else "preorder" if is_preorder_order
            else "pending_manual"
        )

        user.balance = user_balance - total_amount
        user.total_orders += 1
        user.total_spent = _money(user.total_spent) + total_amount

        order = Order(
            telegram_id=user.telegram_id,
            product_id=product.id,
            product_name=product.name,
            delivered_account="\n".join(delivered_accounts) if delivered_accounts else None,
            amount=total_amount,
            quantity=quantity,
            delivery_type=delivery_type,
            is_preorder=is_preorder_order,
            status=status,
            refunded=False
        )
        db.add(order)
        db.flush()  # populate order.id before we build the result dict

        # --- Referral commission -------------------------------
        # Paid immediately for a charged, non-preorder order
        # (completed or pending_manual — money has already changed
        # hands either way). A preorder's commission is deliberately
        # withheld here and paid later, in handlers/admin_orders.py,
        # at the moment the preorder is actually fulfilled.
        referral_commission_paid = None

        if not is_preorder_order and user.referred_by:
            referrer = (
                db.query(User)
                .filter(User.telegram_id == user.referred_by)
                .with_for_update()
                .first()
            )
            if referrer is not None:
                commission = _money(total_amount * REFERRAL_COMMISSION_RATE)
                if commission > 0:
                    referrer.referral_earnings = _money(referrer.referral_earnings) + commission
                    if REFERRAL_CREDIT_TO_BALANCE:
                        referrer.balance = _money(referrer.balance) + commission
                    referral_commission_paid = {
                        "referrer_telegram_id": referrer.telegram_id,
                        "amount": commission,
                    }

        # Alert admins only the moment stock *crosses* the threshold,
        # so a popular product doesn't spam them on every single sale.
        low_stock_alert = None
        if not is_preorder_order and stock_before > threshold >= new_stock:
            low_stock_alert = {
                "product_id": product.id,
                "product_name": product.name,
                "stock": new_stock,
                "threshold": threshold,
            }

        result = {
            "order_id": order.id,
            "icon": product.icon,
            "name": product.name,
            "delivered_accounts": delivered_accounts,
            "balance": user.balance,
            "stock": new_stock,
            "status": status,
            "is_preorder": is_preorder_order,
            "quantity": quantity,
            "total_price": total_amount,
            "low_stock_alert": low_stock_alert,
            "referral_commission_paid": referral_commission_paid,
        }

    # `with transaction()` committed here (or rolled back + re-raised,
    # in which case we never reach this line at all).
    return result


async def _notify_admins_low_stock(bot, alert: dict):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                "⚠️ Low Stock Alert\n\n"
                f"📦 {alert['product_name']} (#{alert['product_id']})\n"
                f"Remaining: {alert['stock']} (alert threshold: {alert['threshold']})"
            )
        except Exception:
            logger.exception("Failed to notify admin %s of low stock", admin_id)


async def _notify_admins_pending_order(bot, buyer_id: int, result: dict):
    kind = "Preorder" if result["is_preorder"] else "Manual Order"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🆕 New {kind} — needs fulfillment\n\n"
                f"🆔 Order #{result['order_id']}\n"
                f"👤 Buyer: <code>{buyer_id}</code>\n"
                f"📦 Product: {result['name']} x{result['quantity']}\n"
                f"💰 Total: ${result['total_price']:.2f}\n\n"
                "Open Admin → Orders → this order → 📤 Deliver to fulfill it.",
                parse_mode="HTML"
            )
        except Exception:
            logger.exception("Failed to notify admin %s of pending order", admin_id)


@router.callback_query(F.data.startswith("confirm_buy_"))
async def confirm_buy(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    telegram_id = callback.from_user.id
    product_id = int(callback.data.split("_")[2])

    lock = await _get_purchase_lock(telegram_id)
    if lock.locked():
        # A previous tap is still being processed — ignore this one
        # instead of letting it become a second order.
        await callback.answer("Your previous order is still being processed…", show_alert=True)
        return

    async with lock:
        data = await state.get_data()
        quantity = data.get(f"qty_{product_id}", 1)

        try:
            result = await asyncio.to_thread(_do_purchase, telegram_id, product_id, quantity)
        except SQLAlchemyError:
            logger.exception(
                "Database error during purchase | telegram_id=%s product_id=%s qty=%s",
                telegram_id, product_id, quantity,
            )
            await callback.message.edit_text(
                "❌ Something went wrong on our end. Please try again in a moment."
            )
            return
        except Exception:
            logger.exception(
                "Unexpected error during purchase | telegram_id=%s product_id=%s qty=%s",
                telegram_id, product_id, quantity,
            )
            await callback.message.edit_text(
                "❌ Purchase failed unexpectedly. Please try again, or contact support "
                "if it keeps happening."
            )
            return

        await state.update_data(**{f"qty_{product_id}": 1})

        if "error" in result:
            await callback.message.edit_text(f"❌ {result['error']}")
            return

        if result["status"] == "completed":
            joined_accounts = "\n".join(
                f"<code>{acc}</code>" for acc in result["delivered_accounts"]
            )
            text = (
                "✅ Purchase Successful!\n\n"
                f"📦 Product:\n{result['icon']} {result['name']} x{result['quantity']}\n\n"
                f"🔑 Delivered:\n\n{joined_accounts}\n\n"
                f"💰 Remaining Balance:\n${result['balance']:.2f}\n\n"
                f"📦 Remaining Stock:\n{result['stock']}"
            )
        elif result["status"] == "preorder":
            text = (
                "📦 Preorder Placed!\n\n"
                f"📦 Product:\n{result['icon']} {result['name']} x{result['quantity']}\n\n"
                f"💰 Charged:\n${result['total_price']:.2f}\n"
                f"💰 Remaining Balance:\n${result['balance']:.2f}\n\n"
                "This item is currently out of stock. We'll message you here as "
                "soon as it's delivered."
            )
        else:  # pending_manual
            text = (
                "⏳ Order Received!\n\n"
                f"📦 Product:\n{result['icon']} {result['name']} x{result['quantity']}\n\n"
                f"💰 Charged:\n${result['total_price']:.2f}\n"
                f"💰 Remaining Balance:\n${result['balance']:.2f}\n\n"
                "This product is delivered manually by our team — you'll get a "
                "message here as soon as it's ready."
            )

        await callback.message.edit_text(text, parse_mode="HTML")

        if result.get("low_stock_alert"):
            await _notify_admins_low_stock(callback.bot, result["low_stock_alert"])

        if result["status"] in ("pending_manual", "preorder"):
            await _notify_admins_pending_order(callback.bot, telegram_id, result)

        commission = result.get("referral_commission_paid")
        if commission:
            try:
                await callback.bot.send_message(
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
