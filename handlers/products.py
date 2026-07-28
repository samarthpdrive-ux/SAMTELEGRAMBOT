import asyncio
from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext

from config import ADMIN_IDS
from database import SessionLocal
from models.product import Product
from models.user import User
from models.order import Order

router = Router()

# How many units a customer can order in one go when there's no real
# stock ceiling (e.g. an out-of-stock preorder). Keeps the + button from
# scrolling to infinity.
PREORDER_MAX_QTY = 10
DEFAULT_LOW_STOCK_THRESHOLD = 3


# =====================================================
# HELPERS (run in thread — keep DB calls off the event loop)
# =====================================================

def _fetch_active_products():
    db = SessionLocal()
    try:
        return (
            db.query(Product)
            .filter(Product.is_active == True)
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


def _accounts_count(product) -> int:
    if not product.file_content:
        return 0
    return len([a for a in product.file_content.splitlines() if a.strip()])


def _get_max_qty(product) -> int:
    """
    The largest quantity we'll let someone select right now, given the
    product's delivery type, live stock, and whether preorder is on.
    """
    delivery_type = (product.delivery_type or "automatic").lower()
    accounts_available = _accounts_count(product)

    if delivery_type == "automatic":
        cap = accounts_available
    elif delivery_type == "manual":
        cap = product.stock or 0
    else:  # hybrid
        cap = max(accounts_available, product.stock or 0)

    if cap <= 0 and product.preorder:
        return PREORDER_MAX_QTY

    return max(cap, 0)


def _get_max_qty_real_stock(product) -> int:
    """Max quantity available from *real* stock only (ignores preorder)."""
    delivery_type = (product.delivery_type or "automatic").lower()
    accounts_available = _accounts_count(product)
    if delivery_type == "automatic":
        return accounts_available
    if delivery_type == "manual":
        return product.stock or 0
    return max(accounts_available, product.stock or 0)


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
    real_stock_available = _get_max_qty_real_stock(product) > 0

    text = (
        f"{product.icon or '📦'} {product.name}\n\n"
        f"📝 Description:\n{product.description or 'No description'}\n\n"
        f"💰 Price: ${float(product.price):.2f}\n"
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
    total = float(product.price) * qty
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

    real_stock_available = _get_max_qty_real_stock(product) > 0

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

    real_stock_available = _get_max_qty_real_stock(product) > 0

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
# PURCHASE
# =====================================================

def _do_purchase(telegram_id: int, product_id: int, quantity: int):
    """All the DB work for a purchase, run in a thread."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        product = db.query(Product).filter(Product.id == product_id).first()

        if not user:
            return {"error": "User not found."}
        if not product:
            return {"error": "Product not found."}
        if not product.is_active:
            return {"error": "Product unavailable."}
        if quantity < 1:
            return {"error": "Quantity must be at least 1."}

        total_price = round(float(product.price) * quantity, 2)

        if float(user.balance) < total_price:
            return {"error": f"Insufficient balance. This order costs ${total_price:.2f}."}

        delivery_type = (product.delivery_type or "automatic").lower()
        threshold = (
            product.low_stock_threshold
            if product.low_stock_threshold is not None
            else DEFAULT_LOW_STOCK_THRESHOLD
        )

        accounts = []
        if product.file_content:
            accounts = [a.strip() for a in product.file_content.splitlines() if a.strip()]
        available = len(accounts)
        stock_before = product.stock or 0

        delivered_accounts = []
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

        status = (
            "completed" if delivered_accounts
            else "preorder" if is_preorder_order
            else "pending_manual"
        )

        user.balance = float(user.balance) - total_price
        user.total_orders += 1
        user.total_spent = float(user.total_spent) + total_price

        order = Order(
            telegram_id=user.telegram_id,
            product_id=product.id,
            product_name=product.name,
            delivered_account="\n".join(delivered_accounts) if delivered_accounts else None,
            amount=total_price,
            quantity=quantity,
            delivery_type=delivery_type,
            is_preorder=is_preorder_order,
            status=status,
            refunded=False
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        # Alert admins only the moment stock *crosses* the threshold, so a
        # popular product doesn't spam them on every single sale.
        low_stock_alert = None
        if not is_preorder_order and stock_before > threshold >= new_stock:
            low_stock_alert = {
                "product_id": product.id,
                "product_name": product.name,
                "stock": new_stock,
                "threshold": threshold,
            }

        return {
            "order_id": order.id,
            "icon": product.icon,
            "name": product.name,
            "delivered_accounts": delivered_accounts,
            "balance": float(user.balance),
            "stock": new_stock,
            "status": status,
            "is_preorder": is_preorder_order,
            "quantity": quantity,
            "total_price": total_price,
            "low_stock_alert": low_stock_alert,
        }

    except Exception as e:
        db.rollback()
        print("BUY ERROR:", e)
        return {"error": "Purchase failed. Please try again."}

    finally:
        db.close()


async def _notify_admins_low_stock(bot, alert: dict):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                "⚠️ Low Stock Alert\n\n"
                f"📦 {alert['product_name']} (#{alert['product_id']})\n"
                f"Remaining: {alert['stock']} (alert threshold: {alert['threshold']})"
            )
        except Exception as e:
            print("LOW STOCK ALERT ERROR:", e)


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
        except Exception as e:
            print("PENDING ORDER NOTIFY ERROR:", e)


@router.callback_query(F.data.startswith("confirm_buy_"))
async def confirm_buy(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    product_id = int(callback.data.split("_")[2])
    data = await state.get_data()
    quantity = data.get(f"qty_{product_id}", 1)

    result = await asyncio.to_thread(
        _do_purchase, callback.from_user.id, product_id, quantity
    )

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
        await _notify_admins_pending_order(callback.bot, callback.from_user.id, result)
