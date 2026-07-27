from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

def get_main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛍 Products",
                    callback_data="products_menu",
                    style="primary"
                ),
                InlineKeyboardButton(
                    text="💰 Deposit",
                    callback_data="deposit_start",
                    style="success"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Referrals",
                    callback_data="referrals_menu",
                    style="primary"
                ),
                InlineKeyboardButton(
                    text="📦 Orders",
                    callback_data="orders_menu",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎫 Support",
                    callback_data="support_ticket",
                    style="primary"
                ),
                InlineKeyboardButton(
                    text="📞 Contact",
                    callback_data="contact_info",
                    style="primary"
                )
            ]
        ]
    )

def get_admin_main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛍 Products",
                    callback_data="products_menu",
                    style="primary"
                ),
                InlineKeyboardButton(
                    text="💰 Deposit",
                    callback_data="deposit_start",
                    style="success"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Referrals",
                    callback_data="referrals_menu",
                    style="primary"
                ),
                InlineKeyboardButton(
                    text="📦 Orders",
                    callback_data="orders_menu",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎫 Support",
                    callback_data="support_ticket",
                    style="primary"
                ),
                InlineKeyboardButton(
                    text="📞 Contact",
                    callback_data="contact_info",
                    style="primary"
                )
            ],
            # Small admin button in corner
            [
                InlineKeyboardButton(
                    text="👑",
                    callback_data="admin_panel",
                    style="danger"
                )
            ]
        ]
    )
