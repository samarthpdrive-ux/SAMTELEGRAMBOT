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
                    callback_data="products_menu"
                ),
                InlineKeyboardButton(
                    text="💰 Deposit",
                    callback_data="deposit_start"
                )
            ],

            [
                InlineKeyboardButton(
                    text="👥 Referrals",
                    callback_data="referrals_menu"
                ),
                InlineKeyboardButton(
                    text="📦 Orders",
                    callback_data="orders_menu"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🎫 Support",
                    callback_data="support_ticket"
                ),
                InlineKeyboardButton(
                    text="📞 Contact",
                    callback_data="contact_info"
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
                    callback_data="products_menu"
                ),
                InlineKeyboardButton(
                    text="💰 Deposit",
                    callback_data="deposit_start"
                )
            ],

            [
                InlineKeyboardButton(
                    text="👥 Referrals",
                    callback_data="referrals_menu"
                ),
                InlineKeyboardButton(
                    text="📦 Orders",
                    callback_data="orders_menu"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🎫 Support",
                    callback_data="support_ticket"
                ),
                InlineKeyboardButton(
                    text="📞 Contact",
                    callback_data="contact_info"
                )
            ],

            # Small admin button in corner
            [
                InlineKeyboardButton(
                    text="👑",
                    callback_data="admin_panel"
                )
            ]
        ]
    )