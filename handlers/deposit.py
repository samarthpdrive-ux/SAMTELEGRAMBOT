import uuid
import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from sqlalchemy.exc import IntegrityError

from states.deposit_states import DepositState

from database import SessionLocal
from models.deposit import Deposit

from services.deposit_checker import verify_deposit

from keyboards.deposit import get_deposit_menu

from config import BEP20_ADDRESS, POLYGON_ADDRESS

logger = logging.getLogger(__name__)

router = Router()

# NOTE: TRC20 and Binance UID deposits have been removed and must not
# be reintroduced. Currently supported: USDT BEP20, USDT Polygon.
NETWORK_ADDRESSES = {
    "BEP20": BEP20_ADDRESS,
    "POLYGON": POLYGON_ADDRESS,
}

NETWORK_CALLBACKS = {
    "deposit_bep20": "BEP20",
    "deposit_polygon": "POLYGON",
}


# =====================================================
# OPEN DEPOSIT MENU
# =====================================================

@router.callback_query(F.data == "deposit_start")
async def deposit_menu(callback: CallbackQuery):
    await callback.message.answer(
        "💰 Select deposit network:",
        reply_markup=get_deposit_menu()
    )
    await callback.answer()


# =====================================================
# NETWORK SELECTION
# =====================================================

@router.callback_query(F.data.in_(NETWORK_CALLBACKS.keys()))
async def select_network(
        callback: CallbackQuery,
        state: FSMContext
):
    network = NETWORK_CALLBACKS[callback.data]

    await state.update_data(network=network)
    await state.set_state(DepositState.waiting_amount)

    await callback.message.answer(
        "💰 Enter deposit amount:"
    )
    await callback.answer()


# =====================================================
# AMOUNT
# =====================================================

@router.message(DepositState.waiting_amount)
async def process_amount(
        message: Message,
        state: FSMContext
):
    try:
        amount = round(float(message.text), 2)
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        await message.answer("❌ Invalid amount.")
        return

    data = await state.get_data()
    network = data.get("network")

    address = NETWORK_ADDRESSES.get(network)

    if not address:
        logger.error(
            "process_amount: no receive address configured for network %s",
            network,
        )
        await message.answer(
            "❌ This network isn't available right now. "
            "Please choose another network."
        )
        await state.clear()
        return

    db = SessionLocal()
    try:
        deposit = Deposit(
            telegram_id=message.from_user.id,
            order_id=str(uuid.uuid4()),
            amount=amount,
            network=network,
            status="waiting_hash"
        )

        db.add(deposit)
        db.commit()
        db.refresh(deposit)

        deposit_id = deposit.id

    except Exception as e:
        logger.exception("process_amount: failed to create deposit: %s", e)
        db.rollback()
        await message.answer(
            "❌ Something went wrong creating your deposit. Please try again."
        )
        return

    finally:
        db.close()

    await state.update_data(deposit_id=deposit_id)

    await message.answer(
        (
            "💰 Deposit Created\n\n"
            "━━━━━━━━━━━━━━\n\n"
            f"Network:\n{network}\n\n"
            f"Amount:\n"
            f"{amount:.2f} USDT\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "Send funds to:\n\n"
            f"<code>{address}</code>\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "After payment send:\n\n"
            "TXID / HASH ID"
        ),
        parse_mode="HTML"
    )

    await state.set_state(DepositState.waiting_txid)


# =====================================================
# TXID
# =====================================================

@router.message(DepositState.waiting_txid)
async def process_txid(
        message: Message,
        state: FSMContext
):
    txid = message.text.strip()

    data = await state.get_data()
    deposit_id = data.get("deposit_id")

    if not deposit_id:
        await message.answer(
            "❌ No deposit in progress. Please start a new deposit."
        )
        await state.clear()
        return

    db = SessionLocal()
    try:
        existing = (
            db.query(Deposit)
            .filter(Deposit.tx_hash == txid)
            .first()
        )

        if existing:
            await message.answer(
                "❌ This TXID has already been used."
            )
            return

        deposit = (
            db.query(Deposit)
            .filter(Deposit.id == deposit_id)
            .first()
        )

        if not deposit:
            await message.answer("❌ Deposit not found.")
            return

        deposit.tx_hash = txid

        try:
            db.commit()
        except IntegrityError:
            # Race condition: two requests submitted the same tx_hash
            # at once — the DB-level unique constraint caught it.
            db.rollback()
            await message.answer(
                "❌ This TXID has already been used."
            )
            return

    finally:
        db.close()

    await message.answer("🔍 Verifying transaction...")

    success = await verify_deposit(deposit_id)

    await state.clear()

    if success:
        await message.answer(
            "✅ Deposit confirmed.\n\n"
            "Balance has been credited."
        )
        return

    # Verification didn't complete instantly. If verify_deposit already
    # set a terminal "failed" state (reverted tx, duplicate, bad hash),
    # leave it alone. Otherwise this just isn't confirmed on-chain yet —
    # move it to "pending" so the background checker keeps retrying,
    # instead of marking it failed and losing it.
    db = SessionLocal()
    try:
        dep = (
            db.query(Deposit)
            .filter(Deposit.id == deposit_id)
            .first()
        )

        if dep and dep.status == "waiting_hash":
            dep.status = "pending"
            db.commit()

    finally:
        db.close()

    await message.answer(
        "⏳ Transaction not confirmed yet.\n\n"
        "It will be verified automatically once it has enough "
        "blockchain confirmations — no need to resend the TXID."
    )