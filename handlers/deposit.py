import re
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
# be reintroduced. Currently supported: USDT BEP20, USDT Polygon, UPI.
NETWORK_ADDRESSES = {
    "BEP20": BEP20_ADDRESS,
    "POLYGON": POLYGON_ADDRESS,
}

NETWORK_CALLBACKS = {
    "deposit_bep20": "BEP20",
    "deposit_polygon": "POLYGON",
    "deposit_upi": "UPI",
}

CRYPTO_NETWORKS = {"BEP20", "POLYGON"}

# Both crypto networks are EVM chains, so a valid tx hash is always
# "0x" + 64 hex characters.
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

# A UPI UTR (per NPCI) is always a 12-digit number. Some apps (FamApp
# and others) instead show their own transaction id, e.g.
# "FMPIB6269486679" (letters then digits) — accept that shape too.
# Kept in sync with services/deposit_checker.py's UTR_RE / TXN_ID_RE.
UTR_RE = re.compile(r"^\d{12}$")
TXN_ID_RE = re.compile(r"^[A-Za-z]{3,10}\d{6,15}$")


def _valid_upi_reference(value: str) -> bool:
    return bool(UTR_RE.match(value) or TXN_ID_RE.match(value))

# Every persistent reply-keyboard button label in the bot. If the user
# taps one of these while mid-deposit-flow (instead of pasting a hash),
# treat it as "leave the flow", not as a tx_hash to save.
# TODO: make sure this list matches ALL your reply-keyboard buttons.
MENU_BUTTON_LABELS = {
    "📦 Manage Products",
    "🛍 Products",
    "💰 Deposit",
    "📜 Orders",
    "🤝 Referrals",
    "🆘 Support",
}


# =====================================================
# OPEN DEPOSIT MENU
# =====================================================

@router.callback_query(F.data == "deposit_start")
async def deposit_menu(callback: CallbackQuery):
    await callback.answer()

    await callback.message.answer(
        "💰 Select deposit method:",
        reply_markup=get_deposit_menu()
    )


# =====================================================
# NETWORK SELECTION
# =====================================================

@router.callback_query(F.data.in_(NETWORK_CALLBACKS.keys()))
async def select_network(
        callback: CallbackQuery,
        state: FSMContext
):
    await callback.answer()

    network = NETWORK_CALLBACKS[callback.data]

    await state.update_data(network=network)
    await state.set_state(DepositState.waiting_amount)

    await callback.message.answer(
        "💰 Enter deposit amount:"
    )


# =====================================================
# AMOUNT
# =====================================================

def _create_deposit(telegram_id: int, amount: float, network: str) -> int:
    """Blocking DB write, run in a thread."""
    db = SessionLocal()
    try:
        deposit = Deposit(
            telegram_id=telegram_id,
            order_id=str(uuid.uuid4()),
            amount=amount,
            network=network,
            status="waiting_hash"
        )
        db.add(deposit)
        db.commit()
        db.refresh(deposit)
        return deposit.id
    finally:
        db.close()


@router.message(DepositState.waiting_amount)
async def process_amount(
        message: Message,
        state: FSMContext
):
    # If the user bails via a menu button instead of typing an amount,
    # leave the flow instead of trying to parse the button label as a number.
    if message.text and message.text.strip() in MENU_BUTTON_LABELS:
        await state.clear()
        return

    try:
        amount = round(float(message.text), 2)
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        await message.answer("❌ Invalid amount.")
        return

    data = await state.get_data()
    network = data.get("network")

    if network == "UPI":
        import config
        upi_id = getattr(config, "UPI_ID", None)

        if not upi_id:
            logger.error("process_amount: UPI_ID not configured")
            await message.answer(
                "❌ This method isn't available right now. "
                "Please choose another method."
            )
            await state.clear()
            return

        pay_to_line = f"Pay to:\n\n<code>{upi_id}</code>"
        amount_line = f"₹{amount:.2f}"
        after_payment_line = (
            "After payment send:\n\n"
            "UTR / Transaction ID\n"
            "(the reference number from your payment app, bank SMS, "
            "or confirmation email — e.g. 123456789012 or FMPIB6269486679)"
        )

    elif network in CRYPTO_NETWORKS:
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

        pay_to_line = f"Send funds to:\n\n<code>{address}</code>"
        amount_line = f"{amount:.2f} USDT"
        after_payment_line = "After payment send:\n\nTXID / HASH ID"

    else:
        logger.error("process_amount: unknown network %s", network)
        await message.answer(
            "❌ Something went wrong. Please start a new deposit."
        )
        await state.clear()
        return

    import asyncio

    try:
        deposit_id = await asyncio.to_thread(
            _create_deposit,
            message.from_user.id,
            amount,
            network,
        )
    except Exception as e:
        logger.exception("process_amount: failed to create deposit: %s", e)
        await message.answer(
            "❌ Something went wrong creating your deposit. Please try again."
        )
        return

    await state.update_data(deposit_id=deposit_id)

    await message.answer(
        (
            "💰 Deposit Created\n\n"
            "━━━━━━━━━━━━━━\n\n"
            f"Method:\n{network}\n\n"
            f"Amount:\n"
            f"{amount_line}\n\n"
            "━━━━━━━━━━━━━━\n\n"
            f"{pay_to_line}\n\n"
            "━━━━━━━━━━━━━━\n\n"
            f"{after_payment_line}"
        ),
        parse_mode="HTML"
    )

    await state.set_state(DepositState.waiting_txid)


# =====================================================
# TXID / UTR
# =====================================================

def _check_duplicate(txid: str) -> bool:
    db = SessionLocal()
    try:
        return (
            db.query(Deposit)
            .filter(Deposit.tx_hash == txid)
            .first()
        ) is not None
    finally:
        db.close()


def _save_txid(deposit_id: int, txid: str) -> str:
    """
    Saves the tx_hash (crypto hash, or UPI UTR) for a deposit.
    Returns "ok", "not_found", or "duplicate".
    """
    db = SessionLocal()
    try:
        deposit = (
            db.query(Deposit)
            .filter(Deposit.id == deposit_id)
            .first()
        )

        if not deposit:
            return "not_found"

        deposit.tx_hash = txid

        try:
            db.commit()
        except IntegrityError:
            # Race condition: two requests submitted the same tx_hash/UTR at once.
            db.rollback()
            return "duplicate"

        return "ok"

    finally:
        db.close()


def _set_pending_if_waiting(deposit_id: int) -> None:
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


@router.message(DepositState.waiting_txid)
async def process_txid(
        message: Message,
        state: FSMContext
):
    import asyncio

    txid = message.text.strip() if message.text else ""

    # User tapped a menu button instead of pasting a hash/UTR — leave
    # the deposit flow instead of saving the button label as one.
    if txid in MENU_BUTTON_LABELS:
        await state.clear()
        return

    data = await state.get_data()
    deposit_id = data.get("deposit_id")
    network = data.get("network")

    if not deposit_id:
        await message.answer(
            "❌ No deposit in progress. Please start a new deposit."
        )
        await state.clear()
        return

    # Validate format BEFORE ever writing it to the DB. This is what
    # stops garbage tx_hash/UTR values from reaching the checkers.
    if network == "UPI":
        if not _valid_upi_reference(txid):
            await message.answer(
                "❌ That doesn't look like a valid UTR / transaction ID.\n\n"
                "It should look like one of these:\n"
                "<code>123456789012</code> (12-digit bank UTR)\n"
                "<code>FMPIB6269486679</code> (app transaction ID)\n\n"
                "Check your payment app / bank SMS / confirmation email "
                "and send the correct one.",
                parse_mode="HTML"
            )
            return  # stay in waiting_txid, ask again
    else:
        if not TX_HASH_RE.match(txid):
            await message.answer(
                "❌ That doesn't look like a valid transaction hash.\n\n"
                "It should look like:\n"
                "<code>0x1234...abcd</code> (66 characters total)\n\n"
                "Please paste the correct TXID/hash.",
                parse_mode="HTML"
            )
            return  # stay in waiting_txid, ask again

    if await asyncio.to_thread(_check_duplicate, txid):
        await message.answer("❌ This has already been used.")
        return

    result = await asyncio.to_thread(_save_txid, deposit_id, txid)

    if result == "not_found":
        await message.answer("❌ Deposit not found.")
        return

    if result == "duplicate":
        await message.answer("❌ This has already been used.")
        return

    await message.answer("🔍 Verifying...")

    success = await verify_deposit(deposit_id)

    await state.clear()

    if success:
        await message.answer(
            "✅ Deposit confirmed.\n\n"
            "Balance has been credited."
        )
        return

    # Verification didn't complete instantly. If verify_deposit already
    # set a terminal "failed" state (reverted tx, duplicate, bad hash/UTR),
    # leave it alone. Otherwise this just isn't confirmed yet — move it
    # to "pending" so the background checker keeps retrying, instead of
    # marking it failed and losing it.
    await asyncio.to_thread(_set_pending_if_waiting, deposit_id)

    if network == "UPI":
        await message.answer(
            "⏳ Payment not matched yet.\n\n"
            "It will be confirmed automatically once we receive the "
            "payment notification — no need to resend the UTR."
        )
    else:
        await message.answer(
            "⏳ Transaction not confirmed yet.\n\n"
            "It will be verified automatically once it has enough "
            "blockchain confirmations — no need to resend the TXID."
        )
