from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton
)


def get_deposit_menu() -> InlineKeyboardMarkup:
    """
    Network/method-selection keyboard shown when a user starts a deposit.
    NOTE: TRC20 and Binance UID deposits have been removed and must
    not be reintroduced. Currently supported: USDT BEP20, USDT Polygon, UPI.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟡 USDT BEP20",
                    callback_data="deposit_bep20"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟣 USDT Polygon",
                    callback_data="deposit_polygon"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🇮🇳 UPI",
                    callback_data="deposit_upi"
                )
            ]
        ]
    )
