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
from models.product import Product
from states.product_states import AddAccounts

router = Router()

print("✅ admin_product_manage imported")


# ==================================================
# TEST
# ==================================================

@router.message(Command("testadmin"))
async def test_admin(
        message: Message
):
    await message.answer(
        "✅ Admin router working"
    )


# ==================================================
# PRODUCT PANEL
# ==================================================

@router.callback_query(
    F.data.startswith("manage_")
)
async def manage_product(
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

    finally:
        db.close()

    if not product:

        await callback.answer(
            "❌ Product not found."
        )
        return

    text = f"""
🆔 ID: {product.id}

📦 Product:
{product.icon} {product.name}

💰 Price:
${float(product.price):.2f}

📊 Stock:
{product.stock}

🏷 Category:
{product.category}

Status:
{"🟢 Enabled" if product.is_active else "🔴 Disabled"}

📝 Description:

{product.description or "No description"}
"""

    markup = InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="➕ Add Accounts",
                    callback_data=
                    f"add_accounts_{product.id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text=
                    "🔴 Disable"
                    if product.is_active
                    else
                    "🟢 Enable",

                    callback_data=
                    f"toggle_{product.id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text="💰 Edit Price",
                    callback_data=
                    f"edit_price_{product.id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text="📦 Edit Stock",
                    callback_data=
                    f"edit_stock_{product.id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text="📝 Description",
                    callback_data=
                    f"desc_{product.id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🗑 Delete Product",
                    callback_data=
                    f"delete_product_{product.id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text="⬅ Back",
                    callback_data="admin_products"
                )
            ]
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=markup
    )

    await callback.answer()


# ==================================================
# ADD ACCOUNTS
# ==================================================

@router.callback_query(
    F.data.startswith(
        "add_accounts_"
    )
)
async def add_accounts(
        callback: CallbackQuery,
        state: FSMContext
):

    pid = int(
        callback.data.split("_")[2]
    )

    await state.update_data(
        product_id=pid
    )

    await state.set_state(
        AddAccounts.accounts
    )

    await callback.message.answer(
        """
Send accounts.

One account per line.

Example:

email1@gmail.com:pass1
email2@gmail.com:pass2
"""
    )

    await callback.answer()


@router.message(
    AddAccounts.accounts
)
async def save_accounts(
        message: Message,
        state: FSMContext
):

    data = await state.get_data()

    db = SessionLocal()

    try:

        product = (
            db.query(Product)
            .filter(
                Product.id ==
                data["product_id"]
            )
            .first()
        )

        if not product:

            await message.answer(
                "❌ Product not found."
            )
            return

        old_accounts = (
            product.file_content
            or ""
        )

        new_accounts = (
            message.text.strip()
        )

        if old_accounts:

            product.file_content = (
                old_accounts
                + "\n"
                + new_accounts
            )

        else:

            product.file_content = (
                new_accounts
            )

        product.stock = len(
            product.file_content
            .splitlines()
        )

        db.commit()

    finally:
        db.close()

    await state.clear()

    await message.answer(
        "✅ Accounts added.",
        reply_markup=
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📦 Product Manager",
                        callback_data=
                        "admin_products"
                    )
                ]
            ]
        )
    )


# ==================================================
# ENABLE / DISABLE
# ==================================================

@router.callback_query(
    F.data.startswith("toggle_")
)
async def toggle_product(
        callback: CallbackQuery
):

    pid = int(
        callback.data.split("_")[1]
    )

    db = SessionLocal()

    try:

        product = (
            db.query(Product)
            .filter(
                Product.id == pid
            )
            .first()
        )

        if product:

            product.is_active = (
                not product.is_active
            )

            db.commit()

    finally:
        db.close()

    callback.data = (
        f"manage_{pid}"
    )

    await manage_product(
        callback
    )


# ==================================================
# HELP COMMANDS
# ==================================================

@router.callback_query(
    F.data.startswith(
        "edit_price_"
    )
)
async def edit_price(
        callback: CallbackQuery
):

    pid = (
        callback.data.split("_")[2]
    )

    await callback.message.answer(
        f"/setprice {pid} 9.99"
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith(
        "edit_stock_"
    )
)
async def edit_stock(
        callback: CallbackQuery
):

    pid = (
        callback.data.split("_")[2]
    )

    await callback.message.answer(
        f"/setstock {pid} 100"
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith(
        "desc_"
    )
)
async def edit_desc(
        callback: CallbackQuery
):

    pid = (
        callback.data.split("_")[1]
    )

    await callback.message.answer(
        f"/setdesc {pid} New Description"
    )

    await callback.answer()


# ==================================================
# SET PRICE
# ==================================================

@router.message(
    Command("setprice")
)
async def set_price(
        message: Message
):

    try:

        _, pid, price = (
            message.text.split()
        )

        db = SessionLocal()

        product = (
            db.query(Product)
            .filter(
                Product.id ==
                int(pid)
            )
            .first()
        )

        if not product:

            await message.answer(
                "❌ Product not found."
            )
            return

        product.price = float(
            price
        )

        db.commit()

        await message.answer(
            "✅ Price updated."
        )

    finally:
        db.close()


# ==================================================
# SET STOCK
# ==================================================

@router.message(
    Command("setstock")
)
async def set_stock(
        message: Message
):

    try:

        _, pid, stock = (
            message.text.split()
        )

        db = SessionLocal()

        product = (
            db.query(Product)
            .filter(
                Product.id ==
                int(pid)
            )
            .first()
        )

        if not product:

            await message.answer(
                "❌ Product not found."
            )
            return

        product.stock = int(
            stock
        )

        db.commit()

        await message.answer(
            "✅ Stock updated."
        )

    finally:
        db.close()


# ==================================================
# SET DESCRIPTION
# ==================================================

@router.message(
    Command("setdesc")
)
async def set_desc(
        message: Message
):

    try:

        parts = (
            message.text.split(
                maxsplit=2
            )
        )

        pid = int(
            parts[1]
        )

        desc = parts[2]

        db = SessionLocal()

        product = (
            db.query(Product)
            .filter(
                Product.id == pid
            )
            .first()
        )

        if not product:

            await message.answer(
                "❌ Product not found."
            )
            return

        product.description = desc

        db.commit()

        await message.answer(
            "✅ Description updated."
        )

    finally:
        db.close()


# ==================================================
# DELETE PRODUCT
# ==================================================

@router.callback_query(
    F.data.startswith(
        "delete_product_"
    )
)
async def delete_product(
        callback: CallbackQuery
):

    pid = int(
        callback.data.split("_")[2]
    )

    db = SessionLocal()

    try:

        product = (
            db.query(Product)
            .filter(
                Product.id == pid
            )
            .first()
        )

        if product:

            db.delete(product)

            db.commit()

    finally:
        db.close()

    await callback.message.edit_text(
        "✅ Product deleted.",
        reply_markup=
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📦 Product Manager",
                        callback_data=
                        "admin_products"
                    )
                ]
            ]
        )
    )

    await callback.answer()