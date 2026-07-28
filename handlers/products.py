import asyncio
from decimal import Decimal, ROUND_HALF_UP
from threading import Lock

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext

from sqlalchemy.exc import SQLAlchemyError

from config import ADMIN_IDS
from database import SessionLocal
from models.product import Product
from models.user import User
from models.order import Order

router = Router()

# -----------------------------------------------------
# CONFIG
# -----------------------------------------------------

PREORDER_MAX_QTY = 10
DEFAULT_LOW_STOCK_THRESHOLD = 3

# Prevent duplicate purchases by same user
_purchase_lock = Lock()

# -----------------------------------------------------
# HELPERS
# -----------------------------------------------------


def money(value) -> Decimal:
    """
    Convert anything into Decimal with 2 decimal precision.
    """
    return Decimal(str(value)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )


def db_price(value):
    """
    Convert Decimal back to float only when needed
    for Telegram display.
    """
    return float(money(value))


# -----------------------------------------------------
# DATABASE HELPERS
# -----------------------------------------------------

def _fetch_active_products():
    db = SessionLocal()

    try:
        return (
            db.query(Product)
            .filter(Product.is_active == True)
            .order_by(Product.id.asc())
            .all()
        )

    finally:
        db.close()


def _fetch_product(product_id: int):

    db = SessionLocal()

    try:

        return (
            db.query(Product)
            .filter(Product.id == product_id)
            .first()
        )

    finally:
        db.close()


# -----------------------------------------------------
# STOCK HELPERS
# -----------------------------------------------------

def _accounts(product):

    if not product.file_content:
        return []

    return [
        line.strip()
        for line in product.file_content.splitlines()
        if line.strip()
    ]


def _accounts_count(product):

    return len(_accounts(product))


def _real_stock(product):

    delivery = (
        product.delivery_type or "automatic"
    ).lower()

    accounts = _accounts_count(product)

    if delivery == "automatic":
        return accounts

    elif delivery == "manual":
        return product.stock or 0

    else:
        return max(
            accounts,
            product.stock or 0
        )


def _max_qty(product):

    stock = _real_stock(product)

    if stock <= 0 and product.preorder:
        return PREORDER_MAX_QTY

    return max(stock, 0)


# -----------------------------------------------------
# DISPLAY HELPERS
# -----------------------------------------------------

def format_price(price):

    return f"${db_price(price):.2f}"


def total_price(price, qty):

    return money(price) * qty

# =====================================================
# PRODUCTS MENU
# =====================================================

@router.callback_query(F.data == "products_menu")
async def products_menu(callback: CallbackQuery):

    await callback.answer()

    products = await asyncio.to_thread(
        _fetch_active_products
    )

    if not products:

        await callback.message.answer(
            "❌ No products available."
        )
        return

    keyboard = []

    for product in products:

        stock = _real_stock(product)

        if stock <= 0 and product.preorder:
            status = "📦 Preorder"

        elif stock <= 0:
            status = "❌"

        elif stock <= 3:
            status = "⚠️"

        else:
            status = "✅"

        keyboard.append([
            InlineKeyboardButton(
                text=f"{product.icon or '📦'} {product.name} {status}",
                callback_data=f"product_{product.id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="⬅ Back",
            callback_data="main_menu"
        )
    ])

    await callback.message.answer(
        "🛍 **Available Products**",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )
    )


# =====================================================
# PRODUCT DETAILS
# =====================================================

@router.callback_query(
    F.data.startswith("product_")
)
async def product_info(
    callback: CallbackQuery
):

    await callback.answer()

    product_id = int(
        callback.data.split("_")[1]
    )

    product = await asyncio.to_thread(
        _fetch_product,
        product_id
    )

    if not product:

        await callback.message.answer(
            "❌ Product not found."
        )
        return

    stock = _real_stock(product)

    preorder = (
        stock <= 0
        and product.preorder
    )

    delivery = (
        product.delivery_type or "automatic"
    ).capitalize()

    text = (
        f"{product.icon or '📦'} <b>{product.name}</b>\n\n"
        f"📝 <b>Description</b>\n"
        f"{product.description or 'No description.'}\n\n"
        f"💰 <b>Price:</b> {format_price(product.price)}\n"
        f"📦 <b>Available:</b> {stock}\n"
        f"🚚 <b>Delivery:</b> {delivery}\n"
        f"🏷 <b>Category:</b> {product.category or 'General'}"
    )

    if preorder:

        text += (
            "\n\n📦 <b>Preorder Available</b>\n"
            "This product is currently out of stock.\n"
            "Purchase now and receive it automatically "
            "when new stock arrives."
        )

    elif stock <= 0:

        text += (
            "\n\n❌ <b>Out of Stock</b>"
        )

    elif stock <= 3:

        text += (
            "\n\n⚠️ <b>Low Stock</b>"
        )

    keyboard = []

    if stock > 0 or preorder:

        keyboard.append([
            InlineKeyboardButton(
                text=(
                    "📦 Preorder"
                    if preorder
                    else "🛒 Buy Now"
                ),
                callback_data=f"select_qty_{product.id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="⬅ Back",
            callback_data="products_menu"
        )
    ])

    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )
    )

# =====================================================
# QUANTITY SELECTOR
# =====================================================

def _qty_text(product, qty: int, preorder: bool):

    total = total_price(
        product.price,
        qty
    )

    title = (
        "📦 Preorder"
        if preorder
        else "🛒 Purchase"
    )

    return (
        f"{title}\n\n"
        f"{product.icon or '📦'} <b>{product.name}</b>\n\n"
        f"📦 Quantity : <b>{qty}</b>\n"
        f"💰 Price : <b>{format_price(product.price)}</b>\n"
        f"💵 Total : <b>{format_price(total)}</b>"
    )


def _qty_keyboard(
    product_id: int,
    qty: int,
    max_qty: int
):

    minus = (
        "noop"
        if qty <= 1
        else f"qty_dec_{product_id}"
    )

    plus = (
        "noop"
        if qty >= max_qty
        else f"qty_inc_{product_id}"
    )

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="➖",
                    callback_data=minus
                ),

                InlineKeyboardButton(
                    text=str(qty),
                    callback_data="noop"
                ),

                InlineKeyboardButton(
                    text="➕",
                    callback_data=plus
                ),
            ],

            [
                InlineKeyboardButton(
                    text="✅ Confirm Purchase",
                    callback_data=f"confirm_buy_{product_id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data=f"cancel_buy_{product_id}"
                )
            ]

        ]
    )


# ---------------------------------------------------
# NO OP
# ---------------------------------------------------

@router.callback_query(
    F.data == "noop"
)
async def noop(callback: CallbackQuery):

    await callback.answer()


# ---------------------------------------------------
# START SELECTOR
# ---------------------------------------------------

@router.callback_query(
    F.data.startswith("select_qty_")
)
async def select_qty(
    callback: CallbackQuery,
    state: FSMContext
):

    await callback.answer()

    product_id = int(
        callback.data.split("_")[2]
    )

    product = await asyncio.to_thread(
        _fetch_product,
        product_id
    )

    if not product:

        await callback.message.answer(
            "❌ Product not found."
        )
        return

    if not product.is_active:

        await callback.message.answer(
            "❌ Product unavailable."
        )
        return

    max_qty = _max_qty(product)

    if max_qty <= 0:

        await callback.message.answer(
            "❌ Out of stock."
        )
        return

    qty = 1

    await state.update_data(
        **{
            f"qty_{product_id}": qty
        }
    )

    preorder = (
        _real_stock(product) <= 0
    )

    await callback.message.answer(

        _qty_text(
            product,
            qty,
            preorder
        ),

        parse_mode="HTML",

        reply_markup=_qty_keyboard(
            product_id,
            qty,
            max_qty
        )

    )


# ---------------------------------------------------
# CHANGE QUANTITY
# ---------------------------------------------------

@router.callback_query(
    F.data.startswith("qty_inc_") |
    F.data.startswith("qty_dec_")
)
async def qty_change(
    callback: CallbackQuery,
    state: FSMContext
):

    await callback.answer()

    action = callback.data.split("_")[1]

    product_id = int(
        callback.data.split("_")[2]
    )

    product = await asyncio.to_thread(
        _fetch_product,
        product_id
    )

    if not product:

        await callback.message.edit_text(
            "❌ Product not found."
        )
        return

    max_qty = _max_qty(product)

    if max_qty <= 0:

        await callback.message.edit_text(
            "❌ Product is out of stock."
        )
        return

    data = await state.get_data()

    qty = data.get(
        f"qty_{product_id}",
        1
    )

    if action == "inc":

        qty += 1

    else:

        qty -= 1

    qty = max(
        1,
        min(qty, max_qty)
    )

    await state.update_data(
        **{
            f"qty_{product_id}": qty
        }
    )

    preorder = (
        _real_stock(product) <= 0
    )

    await callback.message.edit_text(

        _qty_text(
            product,
            qty,
            preorder
        ),

        parse_mode="HTML",

        reply_markup=_qty_keyboard(
            product_id,
            qty,
            max_qty
        )

    )


# ---------------------------------------------------
# CANCEL
# ---------------------------------------------------

@router.callback_query(
    F.data.startswith("cancel_buy_")
)
async def cancel_buy(
    callback: CallbackQuery,
    state: FSMContext
):

    await callback.answer()

    product_id = int(
        callback.data.split("_")[2]
    )

    await state.update_data(
        **{
            f"qty_{product_id}": 1
        }
    )

    await callback.message.edit_text(
        "❌ Purchase cancelled."
    )

# =====================================================
# PURCHASE ENGINE (PART 4A)
# =====================================================

def _do_purchase(
    telegram_id: int,
    product_id: int,
    quantity: int
):
    """
    Safe purchase with transaction + row locking.
    """

    db = SessionLocal()

    try:

        # ---------------------------------------------
        # Lock user & product rows
        # ---------------------------------------------

        user = (
            db.query(User)
            .filter(User.telegram_id == telegram_id)
            .with_for_update()
            .first()
        )

        product = (
            db.query(Product)
            .filter(Product.id == product_id)
            .with_for_update()
            .first()
        )

        if not user:
            return {
                "error": "User not found."
            }

        if not product:
            return {
                "error": "Product not found."
            }

        if not product.is_active:
            return {
                "error": "This product is unavailable."
            }

        if quantity < 1:
            return {
                "error": "Invalid quantity."
            }

        # ---------------------------------------------
        # Money
        # ---------------------------------------------

        price = money(product.price)

        total_amount = (
            price * Decimal(quantity)
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )

        user_balance = money(user.balance)

        if user_balance < total_amount:

            return {
                "error":
                f"Insufficient balance.\n\n"
                f"Required: ${total_amount:.2f}\n"
                f"Balance: ${user_balance:.2f}"
            }

        # ---------------------------------------------
        # Delivery type
        # ---------------------------------------------

        delivery_type = (
            product.delivery_type
            or "automatic"
        ).lower()

        threshold = (
            product.low_stock_threshold
            if product.low_stock_threshold is not None
            else DEFAULT_LOW_STOCK_THRESHOLD
        )

        # ---------------------------------------------
        # Accounts
        # ---------------------------------------------

        accounts = _accounts(product)

        auto_stock = len(accounts)

        manual_stock = (
            product.stock or 0
        )

        delivered_accounts = []

        is_preorder = False

        stock_before = manual_stock

        # ---------------------------------------------
        # AUTOMATIC
        # ---------------------------------------------

        if delivery_type == "automatic":

            if auto_stock >= quantity:

                delivered_accounts = (
                    accounts[:quantity]
                )

                remaining = (
                    accounts[quantity:]
                )

                product.file_content = (
                    "\n".join(remaining)
                )

                product.stock = len(
                    remaining
                )

            elif product.preorder:

                is_preorder = True

            else:

                return {
                    "error":
                    f"Only {auto_stock} accounts left."
                }

        # ---------------------------------------------
        # MANUAL
        # ---------------------------------------------

        elif delivery_type == "manual":

            if manual_stock >= quantity:

                product.stock = (
                    manual_stock - quantity
                )

            elif product.preorder:

                is_preorder = True

            else:

                return {
                    "error":
                    f"Only {manual_stock} left."
                }

        # ---------------------------------------------
        # HYBRID
        # ---------------------------------------------

        else:

            if auto_stock >= quantity:

                delivered_accounts = (
                    accounts[:quantity]
                )

                remaining = (
                    accounts[quantity:]
                )

                product.file_content = (
                    "\n".join(remaining)
                )

                product.stock = len(
                    remaining
                )

            elif manual_stock >= quantity:

                product.stock = (
                    manual_stock - quantity
                )

            elif product.preorder:

                is_preorder = True

            else:

                available = max(
                    auto_stock,
                    manual_stock
                )

                return {
                    "error":
                    f"Only {available} available."
                }

        # ---------------------------------------------
        # Update user
        # ---------------------------------------------

        user.balance = float(
            user_balance - total_amount
        )

        user.total_orders += 1

        user.total_spent = float(
            money(user.total_spent)
            + total_amount
        )

        # status decided in Part 4B
                # ---------------------------------------------
        # Decide Order Status
        # ---------------------------------------------

        if delivered_accounts:

            status = "completed"

        elif is_preorder:

            status = "preorder"

        else:

            status = "pending_manual"

        # ---------------------------------------------
        # Create Order
        # ---------------------------------------------

        order = Order(

            telegram_id=user.telegram_id,

            product_id=product.id,

            product_name=product.name,

            amount=float(total_amount),

            quantity=quantity,

            delivery_type=delivery_type,

            is_preorder=is_preorder,

            delivered_account=(
                "\n".join(delivered_accounts)
                if delivered_accounts
                else None
            ),

            status=status,

            refunded=False

        )

        db.add(order)

        # ---------------------------------------------
        # Commit Everything
        # ---------------------------------------------

        db.commit()

        db.refresh(order)

        # ---------------------------------------------
        # Low Stock Alert
        # ---------------------------------------------

        current_stock = _real_stock(product)

        low_stock_alert = None

        if (

            not is_preorder

            and stock_before > threshold

            and current_stock <= threshold

        ):

            low_stock_alert = {

                "product_id": product.id,

                "product_name": product.name,

                "stock": current_stock,

                "threshold": threshold

            }

        # ---------------------------------------------
        # Success
        # ---------------------------------------------

        return {

            "success": True,

            "order_id": order.id,

            "icon": product.icon,

            "name": product.name,

            "quantity": quantity,

            "status": status,

            "is_preorder": is_preorder,

            "balance": float(user.balance),

            "stock": current_stock,

            "total_price": float(total_amount),

            "delivered_accounts": delivered_accounts,

            "low_stock_alert": low_stock_alert

        }

    except SQLAlchemyError as e:

        db.rollback()

        print("DATABASE ERROR:", e)

        return {

            "error": "Database error occurred."

        }

    except Exception as e:

        db.rollback()

        print("PURCHASE ERROR:", e)

        return {

            "error": "Purchase failed. Please try again."

        }

    finally:

        db.close()

# =====================================================
# LOW STOCK NOTIFICATION
# =====================================================

async def _notify_admins_low_stock(bot, alert: dict):

    if not alert:
        return

    text = (
        "⚠️ <b>LOW STOCK ALERT</b>\n\n"
        f"📦 Product : {alert['product_name']}\n"
        f"🆔 Product ID : {alert['product_id']}\n"
        f"📉 Remaining : {alert['stock']}\n"
        f"⚠️ Threshold : {alert['threshold']}"
    )

    for admin in ADMIN_IDS:

        try:

            await bot.send_message(
                admin,
                text,
                parse_mode="HTML"
            )

        except Exception as e:

            print(
                "LOW STOCK ERROR:",
                e
            )


# =====================================================
# PENDING ORDER NOTIFICATION
# =====================================================

async def _notify_admins_pending_order(
    bot,
    buyer_id: int,
    result: dict
):

    if result["status"] == "preorder":
        order_type = "📦 PREORDER"

    else:
        order_type = "⏳ MANUAL ORDER"

    text = (

        f"{order_type}\n\n"

        f"🆔 Order : #{result['order_id']}\n"

        f"👤 Buyer : "
        f"<code>{buyer_id}</code>\n\n"

        f"📦 Product : "
        f"{result['name']}\n"

        f"📦 Qty : "
        f"{result['quantity']}\n"

        f"💰 Total : "
        f"${result['total_price']:.2f}\n\n"

        "Open Admin Panel → Orders "
        "to complete delivery."

    )

    for admin in ADMIN_IDS:

        try:

            await bot.send_message(
                admin,
                text,
                parse_mode="HTML"
            )

        except Exception as e:

            print(
                "ADMIN NOTIFY ERROR:",
                e
            )


# =====================================================
# CONFIRM PURCHASE
# =====================================================

@router.callback_query(
    F.data.startswith("confirm_buy_")
)
async def confirm_buy(
    callback: CallbackQuery,
    state: FSMContext
):

    await callback.answer()

    product_id = int(
        callback.data.split("_")[2]
    )

    data = await state.get_data()

    quantity = data.get(
        f"qty_{product_id}",
        1
    )

    # Reset selector
    await state.update_data(
        **{
            f"qty_{product_id}": 1
        }
    )

    # Execute purchase
    result = await asyncio.to_thread(

        _do_purchase,

        callback.from_user.id,

        product_id,

        quantity

    )

    if "error" in result:

        await callback.message.edit_text(
            f"❌ {result['error']}"
        )

        return

    # -----------------------------
    # SUCCESS
    # -----------------------------

    if result["status"] == "completed":

        delivered = "\n".join(

            f"<code>{x}</code>"

            for x in result["delivered_accounts"]

        )

        text = (

            "✅ <b>Purchase Successful</b>\n\n"

            f"📦 {result['icon']} "

            f"{result['name']}\n"

            f"📦 Quantity : "

            f"{result['quantity']}\n\n"

            f"🔑 Delivered\n\n"

            f"{delivered}\n\n"

            f"💰 Balance : "

            f"${result['balance']:.2f}"

        )

    elif result["status"] == "preorder":

        text = (

            "📦 <b>Preorder Created</b>\n\n"

            f"{result['icon']} "

            f"{result['name']}\n\n"

            f"Quantity : "

            f"{result['quantity']}\n\n"

            f"Charged : "

            f"${result['total_price']:.2f}\n\n"

            "You'll receive it automatically "

            "when stock is added."

        )

    else:

        text = (

            "⏳ <b>Order Created</b>\n\n"

            f"{result['icon']} "

            f"{result['name']}\n\n"

            f"Quantity : "

            f"{result['quantity']}\n\n"

            "An admin will deliver "

            "your product shortly."

        )

    await callback.message.edit_text(

        text,

        parse_mode="HTML"

    )

    # Notify admins

    if result.get("low_stock_alert"):

        await _notify_admins_low_stock(

            callback.bot,

            result["low_stock_alert"]

        )

    if result["status"] in (

        "pending_manual",

        "preorder"

    ):

        await _notify_admins_pending_order(

            callback.bot,

            callback.from_user.id,

            result

        )
