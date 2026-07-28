from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext

from config import ADMIN_IDS
from database import SessionLocal
from models.product import Product
from states.product_states import AddAccounts

router = Router()

print("✅ admin_product_manage imported")

DELIVERY_TYPES = ["automatic", "manual", "hybrid"]
DELIVERY_LABELS = {
    "automatic": "⚡ Automatic",
    "manual": "🖐 Manual",
    "hybrid": "🔀 Hybrid",
}
DEFAULT_LOW_STOCK_THRESHOLD = 3


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

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

    delivery_type = (product.delivery_type or "automatic").lower()
    threshold = (
        product.low_stock_threshold
        if product.low_stock_threshold is not None
        else DEFAULT_LOW_STOCK_THRESHOLD
    )

    text = f"""
🆔 ID: {product.id}

📦 Product:
{product.icon} {product.name}

💰 Price:
${float(product.price):.2f}

📊 Stock:
{product.stock}

⚠️ Low Stock Alert At:
{threshold}

🚚 Delivery Type:
{DELIVERY_LABELS.get(delivery_type, delivery_type)}

📦 Preorder:
{"🟢 Enabled" if product.preorder else "🔴 Disabled"}

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
                ),
                InlineKeyboardButton(
                    text="📦 Edit Stock",
                    callback_data=
                    f"edit_stock_{product.id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text=f"🚚 Delivery: {DELIVERY_LABELS.get(delivery_type, delivery_type)}",
                    callback_data=
                    f"cycle_delivery_{product.id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text=
                    "📦 Preorder: 🟢 ON"
                    if product.preorder
                    else
                    "📦 Preorder: 🔴 OFF",
                    callback_data=
                    f"toggle_preorder_{product.id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text="⚠️ Edit Low Stock Alert",
                    callback_data=
                    f"edit_threshold_{product.id}"
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

    if callback.message.text is not None:
        await callback.message.edit_text(
            text,
            reply_markup=markup
        )
    else:
        await callback.message.answer(
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

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

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

This adds to stock for Automatic/Hybrid delivery. Stock count is
recalculated from however many lines are left after each sale.
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

    if not is_admin(message.from_user.id):
        return

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
            [
                line for line in
                product.file_content.splitlines()
                if line.strip()
            ]
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
    & ~F.data.startswith("toggle_preorder_")
)
async def toggle_product(
        callback: CallbackQuery
):

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

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
# DELIVERY TYPE (automatic / manual / hybrid)
# ==================================================

@router.callback_query(
    F.data.startswith("cycle_delivery_")
)
async def cycle_delivery_type(
        callback: CallbackQuery
):

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(
        callback.data.split("_")[2]
    )

    db = SessionLocal()

    try:

        product = (
            db.query(Product)
            .filter(Product.id == pid)
            .first()
        )

        if not product:
            await callback.answer("❌ Product not found.")
            return

        current = (product.delivery_type or "automatic").lower()

        try:
            next_index = (DELIVERY_TYPES.index(current) + 1) % len(DELIVERY_TYPES)
        except ValueError:
            next_index = 0

        product.delivery_type = DELIVERY_TYPES[next_index]

        db.commit()

        new_label = DELIVERY_LABELS[product.delivery_type]

    finally:
        db.close()

    await callback.answer(f"🚚 Delivery set to {new_label}")

    callback.data = f"manage_{pid}"
    await manage_product(callback)


# ==================================================
# PREORDER TOGGLE
# ==================================================

@router.callback_query(
    F.data.startswith("toggle_preorder_")
)
async def toggle_preorder(
        callback: CallbackQuery
):

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(
        callback.data.split("_")[2]
    )

    db = SessionLocal()

    try:

        product = (
            db.query(Product)
            .filter(Product.id == pid)
            .first()
        )

        if not product:
            await callback.answer("❌ Product not found.")
            return

        product.preorder = not product.preorder

        db.commit()

        new_state = product.preorder

    finally:
        db.close()

    await callback.answer(
        "📦 Preorder enabled" if new_state else "📦 Preorder disabled"
    )

    callback.data = f"manage_{pid}"
    await manage_product(callback)


# ==================================================
# HELP COMMANDS (buttons that show you the command to type)
# ==================================================

@router.callback_query(
    F.data.startswith(
        "edit_price_"
    )
)
async def edit_price(
        callback: CallbackQuery
):

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = (
        callback.data.split("_")[2]
    )

    await callback.message.answer(
        f"Send:\n/setprice {pid} 9.99"
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

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = (
        callback.data.split("_")[2]
    )

    await callback.message.answer(
        f"Send:\n/setstock {pid} 100"
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith(
        "edit_threshold_"
    )
)
async def edit_threshold(
        callback: CallbackQuery
):

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = (
        callback.data.split("_")[2]
    )

    await callback.message.answer(
        f"Send:\n/setthreshold {pid} 3\n\n"
        "You'll get a Telegram alert whenever stock drops to or below this number."
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

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = (
        callback.data.split("_")[1]
    )

    await callback.message.answer(
        f"Send:\n/setdesc {pid} New Description"
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

    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()

    if len(parts) != 3:
        await message.answer(
            "❌ Usage: /setprice <product_id> <price>\nExample: /setprice 5 9.99"
        )
        return

    _, pid_raw, price_raw = parts

    if not pid_raw.isdigit():
        await message.answer("❌ Product ID must be a number.")
        return

    try:
        price = float(price_raw)
    except ValueError:
        await message.answer("❌ Price must be a number, e.g. 9.99")
        return

    if price < 0:
        await message.answer("❌ Price can't be negative.")
        return

    db = SessionLocal()

    try:

        product = (
            db.query(Product)
            .filter(
                Product.id ==
                int(pid_raw)
            )
            .first()
        )

        if not product:

            await message.answer(
                "❌ Product not found."
            )
            return

        product.price = price

        db.commit()

        await message.answer(
            f"✅ Price updated to ${price:.2f}."
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

    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()

    if len(parts) != 3:
        await message.answer(
            "❌ Usage: /setstock <product_id> <stock>\nExample: /setstock 5 100"
        )
        return

    _, pid_raw, stock_raw = parts

    if not pid_raw.isdigit():
        await message.answer("❌ Product ID must be a number.")
        return

    try:
        stock = int(stock_raw)
    except ValueError:
        await message.answer("❌ Stock must be a whole number.")
        return

    if stock < 0:
        await message.answer("❌ Stock can't be negative.")
        return

    db = SessionLocal()

    try:

        product = (
            db.query(Product)
            .filter(
                Product.id ==
                int(pid_raw)
            )
            .first()
        )

        if not product:

            await message.answer(
                "❌ Product not found."
            )
            return

        delivery_type = (product.delivery_type or "automatic").lower()

        if delivery_type == "automatic":
            await message.answer(
                "ℹ️ This product delivers automatically from the account list, "
                "so stock is derived from how many accounts are loaded "
                "(use ➕ Add Accounts to change it). Setting it manually here "
                "won't survive the next sale or account upload."
            )

        product.stock = stock

        db.commit()

        await message.answer(
            f"✅ Stock updated to {stock}."
        )

    finally:
        db.close()


# ==================================================
# SET LOW STOCK THRESHOLD
# ==================================================

@router.message(
    Command("setthreshold")
)
async def set_threshold(
        message: Message
):

    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()

    if len(parts) != 3:
        await message.answer(
            "❌ Usage: /setthreshold <product_id> <threshold>\nExample: /setthreshold 5 3"
        )
        return

    _, pid_raw, threshold_raw = parts

    if not pid_raw.isdigit():
        await message.answer("❌ Product ID must be a number.")
        return

    try:
        threshold = int(threshold_raw)
    except ValueError:
        await message.answer("❌ Threshold must be a whole number.")
        return

    if threshold < 0:
        await message.answer("❌ Threshold can't be negative.")
        return

    db = SessionLocal()

    try:

        product = (
            db.query(Product)
            .filter(
                Product.id ==
                int(pid_raw)
            )
            .first()
        )

        if not product:

            await message.answer(
                "❌ Product not found."
            )
            return

        product.low_stock_threshold = threshold

        db.commit()

        await message.answer(
            f"✅ Low stock alert threshold set to {threshold}."
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

    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(
        maxsplit=2
    )

    if len(parts) != 3:
        await message.answer(
            "❌ Usage: /setdesc <product_id> <description>"
        )
        return

    _, pid_raw, desc = parts

    if not pid_raw.isdigit():
        await message.answer("❌ Product ID must be a number.")
        return

    db = SessionLocal()

    try:

        product = (
            db.query(Product)
            .filter(
                Product.id == int(pid_raw)
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

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

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
