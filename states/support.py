from aiogram.fsm.state import (
    StatesGroup,
    State
)


class SupportState(
    StatesGroup
):
    waiting_message = State()