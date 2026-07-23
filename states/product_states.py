from aiogram.fsm.state import (
    StatesGroup,
    State
)


class AddProduct(StatesGroup):
    name = State()
    icon = State()
    category = State()
    price = State()
    description = State()
    accounts = State()


class AddAccounts(StatesGroup):
    accounts = State()