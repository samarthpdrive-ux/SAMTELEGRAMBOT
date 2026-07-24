import asyncio
from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from database import SessionLocal
from models.product import Product
from models.user import User
from models.order import Order

router = Router()


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

    text = (
        f"{product.icon or '📦'} {product.name}\n\n"
        f"📝 Description:\n{product.description or 'No description'}\n\n"
        f"💰 Price: ${float(product.price):.2f}\n"
        f"📦 Stock: {product.stock}\n"
        f"🏷 Category: {product.category or 'N/A'}"
    )

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Buy", callback_data=f"buy_{product.id}")]
        ]
    )

    await callback.message.answer(text, reply_markup=markup)


# =====================================================
# BUY PRODUCT
# =====================================================

def _do_purchase(telegram_id: int, product_id: int):
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
        if product.stock <= 0:
            return {"error": "Out of stock."}
        if float(user.balance) < float(product.price):
            return {"error": "Insufficient balance."}

        accounts = []
        if product.file_content:
            accounts = [x.strip() for x in product.file_content.splitlines() if x.strip()]

        if not accounts:
            return {"error": "No accounts available."}

        delivered_account = accounts.pop(0)
        product.file_content = "\n".join(accounts)
        product.stock = len(accounts)

        user.balance = float(user.balance) - float(product.price)
        user.total_orders += 1
        user.total_spent = float(user.total_spent) + float(product.price)

        order = Order(
            telegram_id=user.telegram_id,
            product_id=product.id,
            product_name=product.name,
            delivered_account=delivered_account,
            amount=product.price,
            status="completed",
            refunded=False
        )
        db.add(order)
        db.commit()

        return {
            "icon": product.icon,
            "name": product.name,
            "delivered_account": delivered_account,
            "balance": float(user.balance),
            "stock": product.stock,
        }

    except Exception as e:
        db.rollback()
        print("BUY ERROR:", e)
        return {"error": "Purchase failed."}

    finally:
        db.close()


@router.callback_query(F.data.startswith("buy_"))
async def buy_product(callback: CallbackQuery):
    await callback.answer()

    product_id = int(callback.data.split("_")[1])
    result = await asyncio.to_thread(_do_purchase, callback.from_user.id, product_id)

    if "error" in result:
        await callback.message.answer(result["error"])
        return

    await callback.message.answer(
        (
            "✅ Purchase Successful!\n\n"
            f"📦 Product:\n{result['icon']} {result['name']}\n\n"
            f"🔑 Delivered Account:\n\n<code>{result['delivered_account']}</code>\n\n"
            f"💰 Remaining Balance:\n${result['balance']:.2f}\n\n"
            f"📦 Remaining Stock:\n{result['stock']}"
        ),
        parse_mode="HTML"
    )
