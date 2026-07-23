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
# PRODUCTS MENU
# =====================================================

@router.callback_query(
    F.data == "products_menu"
)
async def products_menu(
        callback: CallbackQuery
):

    db = SessionLocal()

    try:

        products = (
            db.query(Product)
            .filter(
                Product.is_active == True
            )
            .all()
        )

        if not products:

            await callback.answer(
                "No products available.",
                show_alert=True
            )
            return

        keyboard = []

        for product in products:

            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=(
                            f"{product.icon or '📦'} "
                            f"{product.name}"
                        ),
                        callback_data=(
                            f"product_{product.id}"
                        )
                    )
                ]
            )

        markup = InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )

        await callback.message.answer(
            "🛍 Available Products:",
            reply_markup=markup
        )

    finally:

        db.close()

    await callback.answer()


# =====================================================
# PRODUCT DETAILS
# =====================================================

@router.callback_query(
    F.data.startswith("product_")
)
async def product_info(
        callback: CallbackQuery
):

    product_id = int(
        callback.data.split("_")[1]
    )

    db = SessionLocal()

    try:

        product = (
            db.query(Product)
            .filter(
                Product.id == product_id
            )
            .first()
        )

        if not product:

            await callback.answer(
                "Product not found.",
                show_alert=True
            )
            return

        text = (
            f"{product.icon or '📦'} "
            f"{product.name}\n\n"
            f"📝 Description:\n"
            f"{product.description or 'No description'}\n\n"
            f"💰 Price: "
            f"${float(product.price):.2f}\n"
            f"📦 Stock: "
            f"{product.stock}\n"
            f"🏷 Category: "
            f"{product.category or 'N/A'}"
        )

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🛒 Buy",
                        callback_data=(
                            f"buy_{product.id}"
                        )
                    )
                ]
            ]
        )

        await callback.message.answer(
            text,
            reply_markup=markup
        )

    finally:

        db.close()

    await callback.answer()


# =====================================================
# BUY PRODUCT
# =====================================================

@router.callback_query(
    F.data.startswith("buy_")
)
async def buy_product(
        callback: CallbackQuery
):

    product_id = int(
        callback.data.split("_")[1]
    )

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

        product = (
            db.query(Product)
            .filter(
                Product.id ==
                product_id
            )
            .first()
        )

        if not user:

            await callback.answer(
                "User not found.",
                show_alert=True
            )
            return

        if not product:

            await callback.answer(
                "Product not found.",
                show_alert=True
            )
            return

        if not product.is_active:

            await callback.answer(
                "Product unavailable.",
                show_alert=True
            )
            return

        if product.stock <= 0:

            await callback.answer(
                "Out of stock.",
                show_alert=True
            )
            return

        if float(user.balance) < float(product.price):

            await callback.answer(
                "Insufficient balance.",
                show_alert=True
            )
            return

        # ==========================================
        # GET ACCOUNTS
        # ==========================================

        accounts = []

        if product.file_content:

            accounts = [
                x.strip()
                for x in
                product.file_content.splitlines()
                if x.strip()
            ]

        if not accounts:

            await callback.answer(
                "No accounts available.",
                show_alert=True
            )
            return

        # ==========================================
        # DELIVER ACCOUNT
        # ==========================================

        delivered_account = (
            accounts.pop(0)
        )

        product.file_content = (
            "\n".join(accounts)
        )

        product.stock = len(accounts)

        # ==========================================
        # USER BALANCE
        # ==========================================

        user.balance = (
            float(user.balance)
            - float(product.price)
        )

        user.total_orders += 1

        user.total_spent = (
            float(user.total_spent)
            + float(product.price)
        )

        # ==========================================
        # SAVE ORDER
        # ==========================================

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

        # ==========================================
        # SUCCESS MESSAGE
        # ==========================================

        await callback.message.answer(
            (
                "✅ Purchase Successful!\n\n"
                f"📦 Product:\n"
                f"{product.icon} "
                f"{product.name}\n\n"
                f"🔑 Delivered Account:\n\n"
                f"<code>"
                f"{delivered_account}"
                f"</code>\n\n"
                f"💰 Remaining Balance:\n"
                f"${float(user.balance):.2f}\n\n"
                f"📦 Remaining Stock:\n"
                f"{product.stock}"
            ),
            parse_mode="HTML"
        )

    except Exception as e:

        db.rollback()

        print(
            "BUY ERROR:",
            e
        )

        await callback.answer(
            "Purchase failed.",
            show_alert=True
        )

    finally:

        db.close()

    await callback.answer()