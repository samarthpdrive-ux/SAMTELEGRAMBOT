from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext

from database import SessionLocal
from config import ADMIN_IDS
from models.product import Product
from states.product_states import AddProduct

router = Router()


@router.callback_query(
    F.data == "create_product"
)
async def add_product(
        callback: CallbackQuery,
        state: FSMContext
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    await state.clear()

    await state.set_state(
        AddProduct.name
    )

    await callback.message.answer(
        """
📦 Create New Product

Enter product name:

Example:
Netflix Premium
"""
    )

    await callback.answer()


@router.message(
    AddProduct.name
)
async def product_name(
        message: Message,
        state: FSMContext
):

    await state.update_data(
        name=message.text.strip()
    )

    await state.set_state(
        AddProduct.icon
    )

    await message.answer(
        "Enter icon:"
    )


@router.message(
    AddProduct.icon
)
async def product_icon(
        message: Message,
        state: FSMContext
):

    await state.update_data(
        icon=message.text.strip()
    )

    await state.set_state(
        AddProduct.category
    )

    await message.answer(
        "Enter category:"
    )


@router.message(
    AddProduct.category
)
async def product_category(
        message: Message,
        state: FSMContext
):

    await state.update_data(
        category=message.text.strip()
    )

    await state.set_state(
        AddProduct.price
    )

    await message.answer(
        "Enter price:"
    )


@router.message(
    AddProduct.price
)
async def product_price(
        message: Message,
        state: FSMContext
):

    try:
        price = float(
            message.text
        )
    except:

        await message.answer(
            "Invalid price."
        )
        return

    await state.update_data(
        price=price
    )

    await state.set_state(
        AddProduct.description
    )

    await message.answer(
        "Enter description:"
    )


@router.message(
    AddProduct.description
)
async def product_description(
        message: Message,
        state: FSMContext
):

    await state.update_data(
        description=message.text
    )

    await state.set_state(
        AddProduct.accounts
    )

    await message.answer(
        """
Send accounts.

One per line.

email1:pass1
email2:pass2
"""
    )


@router.message(
    AddProduct.accounts
)
async def save_product(
        message: Message,
        state: FSMContext
):

    data = await state.get_data()

    accounts = [

        x.strip()

        for x in
        message.text.splitlines()

        if x.strip()
    ]

    db = SessionLocal()

    try:

        product = Product(
            name=data["name"],
            icon=data["icon"],
            category=data["category"],
            description=data["description"],
            price=data["price"],
            stock=len(accounts),
            file_content="\n".join(accounts),
            is_active=True
        )

        db.add(product)

        db.commit()

        db.refresh(product)

    finally:
        db.close()

    await state.clear()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="📦 Product Manager",
                    callback_data="admin_products"
                )
            ],

            [
                InlineKeyboardButton(
                    text="➕ Create Another",
                    callback_data="create_product"
                )
            ]
        ]
    )

    await message.answer(
        f"""
✅ Product Created

🆔 ID: {product.id}

📦 {product.icon}
{product.name}

💰 ${float(product.price):.2f}

📊 Stock:
{product.stock}
""",
        reply_markup=keyboard
    )