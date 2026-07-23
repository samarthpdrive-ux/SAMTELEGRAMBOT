from aiogram.fsm.state import (
    State,
    StatesGroup
)


class DepositState(
    StatesGroup
):
    waiting_amount = State()
    waiting_txid = State()