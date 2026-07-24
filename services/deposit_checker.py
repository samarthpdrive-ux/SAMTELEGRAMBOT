"""
services/deposit_checker.py

USDT Deposit Checker

Supports:
- BEP20 (BSC)
- Polygon

Verification:
- Etherscan V2 API
- Receipt parsing
- Duplicate protection

FIX NOTES:
- Invalid tx_hash / unknown network deposits are marked "failed"
  immediately instead of being left "pending" forever. Previously,
  a deposit created with a bad tx_hash (e.g. a stray menu-button
  label like "📦 Manage Products" instead of a real hash) would sit
  in "pending" and get re-checked + re-logged as an error every
  CHECK_INTERVAL seconds forever, spamming logs indefinitely. Now it
  self-terminates into "failed" on the first check.
- The real root cause (a bot input handler saving arbitrary text as
  tx_hash when the user leaves the deposit flow) should ALSO be fixed
  in handlers/deposit.py — validate the tx_hash format there before
  ever writing it to the Deposit row.
- verify_deposit() was rewritten for clearer, single-path session
  handling (one DB session per call, always closed exactly once).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Union

import requests

from database import SessionLocal
from models.deposit import Deposit
from models.user import User

import config

logger = logging.getLogger(__name__)

# =====================================================
# SETTINGS
# =====================================================

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"

CHECK_INTERVAL = 15

HTTP_TIMEOUT = 20

API_KEY = getattr(config, "ETHERSCAN_API_KEY", "")

# =====================================================
# YOUR WALLETS
# =====================================================

BEP20_ADDRESS = getattr(config, "BEP20_ADDRESS", "").lower()

POLYGON_ADDRESS = getattr(config, "POLYGON_ADDRESS", "").lower()

# =====================================================
# USDT CONTRACTS
# =====================================================

USDT_BSC = "0x55d398326f99059ff775485246999027b3197955".lower()

USDT_POLYGON = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f".lower()

TRANSFER_TOPIC = (
    "0xddf252ad"
    "1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

# =====================================================
# CHAIN CONFIG
# =====================================================

@dataclass
class Chain:

    name: str

    chainid: int

    wallet: str

    contract: str

    decimals: int


CHAINS = {

    "BEP20": Chain(
        "BEP20",
        56,
        BEP20_ADDRESS,
        USDT_BSC,
        18,
    ),

    "POLYGON": Chain(
        "POLYGON",
        137,
        POLYGON_ADDRESS,
        USDT_POLYGON,
        6,
    ),
}

# =====================================================
# HELPERS
# =====================================================

def valid_hash(tx_hash: str) -> bool:
    """Validate transaction hash format."""
    if tx_hash is None:
        return False
    if not isinstance(tx_hash, str):
        return False
    return (
        tx_hash.startswith("0x")
        and len(tx_hash) == 66
    )


def raw_to_amount(value: int, decimals: int) -> Decimal:
    """Convert raw integer amount to Decimal."""
    return Decimal(value) / Decimal(10 ** decimals)


async def etherscan_receipt(
    chain: Chain,
    tx_hash: str,
) -> Optional[dict]:
    """Fetch transaction receipt from Etherscan V2 API."""

    params = {
        "chainid": chain.chainid,
        "module": "proxy",
        "action": "eth_getTransactionReceipt",
        "txhash": tx_hash,
        "apikey": API_KEY,
    }

    try:
        response = await asyncio.to_thread(
            requests.get,
            ETHERSCAN_URL,
            params=params,
            timeout=HTTP_TIMEOUT,
            headers={
                "Accept": "application/json"
            },
        )
        response.raise_for_status()
        data = response.json()

    except Exception as e:
        logger.error(
            "[%s] API Error for tx %s: %s",
            chain.name,
            tx_hash,
            e,
        )
        return None

    if data.get("status") == "0":
        logger.error(
            "[%s] API returned error: %s",
            chain.name,
            data.get("message", "Unknown error"),
        )
        return None

    result = data.get("result")

    if result is None:
        return None

    if isinstance(result, dict):
        return result

    return None


# =====================================================
# RECEIPT PARSER
# =====================================================

def _decode_address(topic: str) -> str:
    """Converts a 32-byte indexed topic into an Ethereum address."""
    if not topic:
        return ""

    topic = topic.lower().replace("0x", "")

    return "0x" + topic[-40:]


def _decode_uint(hex_value: str) -> int:
    """Converts hex string data into integer."""
    if not hex_value:
        return 0

    return int(hex_value, 16)


def parse_transfer(chain: Chain, receipt: dict) -> Optional[dict]:
    """
    Search receipt logs for a valid USDT Transfer event
    directed to our wallet.
    """

    logs = receipt.get("logs", [])

    for log in logs:

        contract = log.get("address", "").lower()

        if contract != chain.contract:
            continue

        topics = log.get("topics", [])

        if len(topics) < 3:
            continue

        if not topics[0].lower().startswith(TRANSFER_TOPIC):
            continue

        sender = _decode_address(topics[1])

        receiver = _decode_address(topics[2])

        if receiver.lower() != chain.wallet:
            continue

        amount_raw = _decode_uint(log.get("data", "0x0"))

        amount = raw_to_amount(
            amount_raw,
            chain.decimals,
        )

        return {
            "sender": sender,
            "receiver": receiver,
            "amount": amount,
            "contract": contract,
        }

    return None


# =====================================================
# RECEIPT VALIDATION
# =====================================================

async def verify_transaction(
    chain: Chain,
    tx_hash: str,
) -> Optional[Union[dict, bool]]:
    """
    Verify a transaction using the Etherscan V2 API.

    Returns:
        None  -> not confirmed yet (API unavailable or tx not indexed)
        False -> transaction failed or no valid USDT transfer
        dict  -> confirmed transfer details
    """

    receipt = await etherscan_receipt(
        chain,
        tx_hash,
    )

    if receipt is None:
        return None

    status = receipt.get("status")

    if status != "0x1":
        logger.info(
            "[%s] %s failed",
            chain.name,
            tx_hash,
        )
        return False

    transfer = parse_transfer(
        chain,
        receipt,
    )

    if transfer is None:
        logger.info(
            "[%s] %s has no valid USDT transfer",
            chain.name,
            tx_hash,
        )
        return False

    transfer["receipt"] = receipt

    return transfer


# =====================================================
# DATABASE HELPERS
# =====================================================

def tx_already_processed(tx_hash: str) -> bool:
    """
    Returns True if another completed deposit already
    used this transaction hash.
    """

    db = SessionLocal()

    try:
        dep = (
            db.query(Deposit)
            .filter(
                Deposit.tx_hash == tx_hash,
                Deposit.status == "completed",
            )
            .first()
        )

        return dep is not None

    finally:
        db.close()


def credit_user(
    db,
    deposit: Deposit,
    amount: Decimal,
) -> bool:
    """Credits the user's balance."""

    user = (
        db.query(User)
        .filter(User.telegram_id == deposit.telegram_id)
        .first()
    )

    if user is None:
        logger.error(
            "User %s not found",
            deposit.telegram_id,
        )
        return False

    user.balance += float(amount)

    if hasattr(user, "total_deposit"):
        user.total_deposit += float(amount)

    deposit.amount = float(amount)
    deposit.status = "completed"

    db.commit()

    logger.info(
        "Deposit credited | User=%s Amount=%s",
        user.telegram_id,
        amount,
    )

    return True


def mark_deposit_failed(deposit_id: int, reason: str) -> None:
    """
    Mark a pending deposit as "failed" so the background loop stops
    retrying it and spamming logs. Used whenever a deposit is found
    to be structurally invalid (bad tx_hash, unknown network, etc.)
    rather than "not yet confirmed on-chain".
    """

    db = SessionLocal()

    try:
        dep = db.get(Deposit, deposit_id)

        if dep is None:
            return

        if dep.status != "pending":
            # Already resolved by someone else — don't clobber it.
            return

        dep.status = "failed"
        db.commit()

        logger.warning(
            "Deposit %s auto-failed: %s",
            deposit_id,
            reason,
        )

    except Exception:
        db.rollback()
        logger.exception(
            "Could not auto-fail deposit %s",
            deposit_id,
        )

    finally:
        db.close()


def _fail_deposit(db, deposit_id: int, reason: str) -> None:
    """Mark a deposit failed using an already-open session."""
    try:
        dep = db.get(Deposit, deposit_id)
        if dep and dep.status not in ("completed", "failed"):
            dep.status = "failed"
            db.commit()
            logger.warning("Deposit %s auto-failed: %s", deposit_id, reason)
    except Exception:
        db.rollback()
        logger.exception("Could not auto-fail deposit %s", deposit_id)


# =====================================================
# VERIFY ONE DEPOSIT
# =====================================================

async def verify_deposit(deposit_or_id: Union[Deposit, int]) -> Optional[bool]:
    """
    Verify a deposit by Deposit object or deposit ID.

    Returns:
        True  -> deposit verified and credited successfully
        False -> deposit failed or invalid
        None  -> deposit still pending (API unavailable or tx not indexed yet)
    """

    db = SessionLocal()

    try:
        # ── Resolve the deposit row in THIS session ──
        if isinstance(deposit_or_id, int):
            deposit_id = deposit_or_id
        else:
            deposit_id = deposit_or_id.id

        deposit = db.get(Deposit, deposit_id)

        if deposit is None:
            logger.error("Deposit %s not found", deposit_id)
            return False

        if deposit.status in ("completed", "failed"):
            # Already resolved (e.g. by a concurrent check) — nothing to do.
            return deposit.status == "completed"

        # ── Validate tx_hash ──
        if not isinstance(deposit.tx_hash, str):
            logger.error(
                "Deposit %s has invalid tx_hash type: %s",
                deposit.id,
                type(deposit.tx_hash),
            )
            _fail_deposit(
                db, deposit.id,
                f"tx_hash not a string (got {type(deposit.tx_hash).__name__})",
            )
            return False

        if not valid_hash(deposit.tx_hash):
            logger.error(
                "Deposit %s has invalid tx_hash: %s",
                deposit.id,
                deposit.tx_hash,
            )
            _fail_deposit(
                db, deposit.id,
                f"tx_hash failed format check: {deposit.tx_hash!r}",
            )
            return False

        # ── Validate network ──
        network = deposit.network.upper() if deposit.network else ""

        if network not in CHAINS:
            logger.error(
                "Unknown network '%s' for deposit %s",
                network,
                deposit.id,
            )
            _fail_deposit(db, deposit.id, f"unknown network {network!r}")
            return False

        chain = CHAINS[network]

        # ── Ask Etherscan ──
        verification = await verify_transaction(chain, deposit.tx_hash)

        if verification is None:
            # Not confirmed / indexed yet — leave as pending, try again later.
            return None

        if verification is False:
            deposit.status = "failed"
            db.commit()
            return False

        # ── Duplicate completed transaction check ──
        duplicate = (
            db.query(Deposit)
            .filter(
                Deposit.tx_hash == deposit.tx_hash,
                Deposit.status == "completed",
                Deposit.id != deposit.id,
            )
            .first()
        )

        if duplicate:
            deposit.status = "failed"
            db.commit()
            logger.warning(
                "Duplicate tx %s for deposit %s",
                deposit.tx_hash,
                deposit.id,
            )
            return False

        # ── Credit the user ──
        return credit_user(db, deposit, verification["amount"])

    except Exception:
        db.rollback()
        logger.exception(
            "Error verifying deposit %s",
            deposit_id if "deposit_id" in locals() else deposit_or_id,
        )
        return False

    finally:
        db.close()


# =====================================================
# CHECK PENDING DEPOSITS
# =====================================================

async def check_pending_deposits():
    """Scan all pending deposits and verify them."""

    db = SessionLocal()

    try:
        deposit_ids = [
            dep_id for (dep_id,) in
            db.query(Deposit.id)
            .filter(Deposit.status == "pending")
            .all()
        ]

        logger.info(
            "Checking %s pending deposits...",
            len(deposit_ids),
        )

    finally:
        db.close()

    for deposit_id in deposit_ids:
        try:
            await verify_deposit(deposit_id)

        except Exception:
            logger.exception(
                "Failed checking deposit %s",
                deposit_id,
            )


# =====================================================
# CHECK SINGLE TRANSACTION
# =====================================================

async def check_single_transaction(tx_hash: str, network: str) -> Optional[Union[dict, bool]]:
    """
    Helper that verifies one transaction without the
    background loop.

    Returns:
        None  -> not confirmed yet
        False -> invalid/failed transaction
        dict  -> confirmed transfer details
    """

    network = network.upper()

    if network not in CHAINS:
        return None

    return await verify_transaction(
        CHAINS[network],
        tx_hash,
    )


# =====================================================
# BACKGROUND LOOP
# =====================================================

async def deposit_checker_loop():

    logger.info("--------------------------------")
    logger.info("Deposit checker started")
    logger.info("--------------------------------")

    while True:
        try:
            await check_pending_deposits()

        except Exception:
            logger.exception(
                "Deposit checker crashed"
            )

        await asyncio.sleep(CHECK_INTERVAL)


# =====================================================
# STARTER
# =====================================================

def start_checker():
    """
    Creates the background task.
    Call once during bot startup.
    """

    return asyncio.create_task(
        deposit_checker_loop()
    )
