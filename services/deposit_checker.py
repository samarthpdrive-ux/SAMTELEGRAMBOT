"""
services/deposit_checker.py

Deposit Checker (combined: crypto + UPI)

Supports:
- BEP20 (BSC)      -> Etherscan V2 API, on-chain receipt verification
- Polygon          -> Etherscan V2 API, on-chain receipt verification
- UPI              -> Gmail IMAP, matches a user-submitted reference
                      (bank UTR, e.g. "421512345678", OR an app txn id
                      like FamApp's "FMPIB6269486679") against payment
                      notification emails

Both paths funnel into the same verify_deposit() / return convention
(None = still pending, False = failed, dict = confirmed transfer),
so the shared logic (duplicate check, credit_user) only exists once.

SETUP REQUIRED (config.py) for UPI:
    IMAP_EMAIL             - Gmail address receiving FamApp alerts
    IMAP_APP_PASSWORD      - 16-char Gmail App Password (load from an
                              env var, never hardcode/commit it)
    FAMAPP_SENDER_EMAIL    - the EXACT "From:" address FamApp payment
                              emails come from. Open one real email ->
                              "Show original" -> copy the From address
                              verbatim. Guessing wrong = matches
                              silently never happen.
    UPI_ID                 - your VPA shown to the user to pay to
    IMAP_LOOKBACK_DAYS     - optional, defaults to 2

NOTE ON REGEXES:
    UTR_LABEL_RE / AMOUNT_RE below are a best-effort guess at how a
    FamApp payment-received email is formatted. Paste a real sample
    email (redacted) and these can be tightened to match exactly.

FIX NOTES:
- Invalid tx_hash/UTR or unknown-network deposits are marked "failed"
  immediately instead of being left "pending" forever, which would
  otherwise get re-checked + re-logged as an error every
  CHECK_INTERVAL seconds indefinitely.
- The root cause (a bot input handler saving arbitrary text as
  tx_hash when the user leaves the deposit flow) is ALSO fixed in
  handlers/deposit.py — format is validated there before ever writing
  to the Deposit row.
- verify_deposit() uses one DB session per call, always closed exactly
  once.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.header import decode_header
from typing import Optional, Union

import requests

from database import SessionLocal
from models.deposit import Deposit
from models.user import User

import config

logger = logging.getLogger(__name__)

# =====================================================
# SETTINGS — CRYPTO
# =====================================================

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"

CHECK_INTERVAL = 15

HTTP_TIMEOUT = 20

API_KEY = getattr(config, "ETHERSCAN_API_KEY", "")

BEP20_ADDRESS = getattr(config, "BEP20_ADDRESS", "").lower()

POLYGON_ADDRESS = getattr(config, "POLYGON_ADDRESS", "").lower()

USDT_BSC = "0x55d398326f99059ff775485246999027b3197955".lower()

USDT_POLYGON = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f".lower()

TRANSFER_TOPIC = (
    "0xddf252ad"
    "1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

UPI_NETWORK = "UPI"

# =====================================================
# SETTINGS — UPI / IMAP
# =====================================================

IMAP_HOST = getattr(config, "IMAP_HOST", "imap.gmail.com")

IMAP_EMAIL = getattr(config, "IMAP_EMAIL", "")

IMAP_APP_PASSWORD = getattr(config, "IMAP_APP_PASSWORD", "")

FAMAPP_SENDER_EMAIL = getattr(config, "FAMAPP_SENDER_EMAIL", "")

UPI_ID = getattr(config, "UPI_ID", "")

IMAP_LOOKBACK_DAYS = getattr(config, "IMAP_LOOKBACK_DAYS", 2)

# A UPI UTR (per NPCI) is always a 12-digit number. Kept in sync with
# handlers/deposit.py's UTR_RE.
UTR_RE = re.compile(r"^\d{12}$")

# Some apps (FamApp and others) don't surface the bank UTR to the user
# at all — instead they show their own transaction ID, e.g.
# "FMPIB6269486679" (a few letters, then digits). Accept that shape too.
TXN_ID_RE = re.compile(r"^[A-Za-z]{3,10}\d{6,15}$")

# Looks for a UTR/txn-id near a UTR/RRN/reference/txn-id label.
# Captures either a 12-digit UTR or a letters+digits app txn id.
# TUNE THIS against a real FamApp email sample.
UTR_LABEL_RE = re.compile(
    r"(?:UTR(?:\s*No\.?)?|UPI\s*Ref(?:erence)?(?:\s*No\.?)?|RRN|"
    r"Txn\s*Ref(?:erence)?|Transaction\s*ID|Txn(?:\s*ID)?)"
    r"\D{0,10}([A-Za-z0-9]{8,20})",
    re.IGNORECASE,
)

# Fallback 1: any bare 12-digit number in the email.
UTR_FALLBACK_RE = re.compile(r"\b(\d{12})\b")

# Fallback 2: any bare app-style txn id (letters then digits) in the
# email, e.g. "FMPIB6269486679". Used only if the two above find
# nothing.
TXN_ID_FALLBACK_RE = re.compile(r"\b([A-Za-z]{3,10}\d{6,15})\b")

# Looks for an amount like "Rs. 500", "₹500.00", "INR 1,200"
AMOUNT_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
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
# HELPERS — CRYPTO
# =====================================================

def valid_hash(tx_hash: str) -> bool:
    """Validate an EVM transaction hash format (BEP20 / Polygon only)."""
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
# HELPERS — UPI
# =====================================================

def valid_utr(utr: str) -> bool:
    """
    Validate a user-submitted UPI reference. Accepts either a bank UTR
    (12 digits) or an app-generated transaction id like
    "FMPIB6269486679" (letters followed by digits). Kept in sync with
    handlers/deposit.py's UTR_RE / TXN_ID_RE.
    """
    if not isinstance(utr, str):
        return False
    return bool(UTR_RE.match(utr) or TXN_ID_RE.match(utr))


def _decode_text(text) -> str:
    if text is None:
        return ""

    decoded = decode_header(text)
    result = ""

    for value, encoding in decoded:
        if isinstance(value, bytes):
            result += value.decode(encoding or "utf-8", errors="ignore")
        else:
            result += value

    return result


def _get_body(msg: email.message.Message) -> str:
    """Extract the plain-text body from a (possibly multipart) email."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition"))

            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="ignore")
        return ""

    payload = msg.get_payload(decode=True)
    return payload.decode(errors="ignore") if payload else ""


def _extract_utr(text: str) -> Optional[str]:
    if not text:
        return None

    match = UTR_LABEL_RE.search(text)
    if match:
        return match.group(1)

    match = UTR_FALLBACK_RE.search(text)
    if match:
        return match.group(1)

    match = TXN_ID_FALLBACK_RE.search(text)
    if match:
        return match.group(1)

    return None


def _extract_amount(text: str) -> Optional[Decimal]:
    if not text:
        return None

    match = AMOUNT_RE.search(text)
    if not match:
        return None

    raw = match.group(1).replace(",", "")

    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _fetch_famapp_matches() -> dict:
    """
    Blocking IMAP call. Connects to Gmail, scans recent emails from
    FamApp, and returns a dict of {utr: amount} for every payment
    notification found in the lookback window.

    Run this via asyncio.to_thread — never call it directly from an
    async function.
    """
    if not IMAP_EMAIL or not IMAP_APP_PASSWORD:
        logger.error("upi: IMAP_EMAIL / IMAP_APP_PASSWORD not configured")
        return {}

    if not FAMAPP_SENDER_EMAIL:
        logger.error("upi: FAMAPP_SENDER_EMAIL not configured")
        return {}

    matches: dict = {}

    mail = None

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(IMAP_EMAIL, IMAP_APP_PASSWORD)
        mail.select("INBOX")

        since_date = (
            datetime.now() - timedelta(days=IMAP_LOOKBACK_DAYS)
        ).strftime("%d-%b-%Y")

        status, data = mail.search(
            None,
            f'(FROM "{FAMAPP_SENDER_EMAIL}" SINCE {since_date})',
        )

        if status != "OK":
            logger.error("upi: IMAP search failed: %s", status)
            return {}

        email_ids = data[0].split()

        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, "(RFC822)")

            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = _decode_text(msg.get("Subject"))
            body = _get_body(msg)
            full_text = f"{subject}\n{body}"

            utr = _extract_utr(full_text)
            if not utr:
                continue

            amount = _extract_amount(full_text)
            if amount is None:
                logger.warning(
                    "upi: found UTR %s but couldn't parse an amount from "
                    "the email — check AMOUNT_RE against this email's format",
                    utr,
                )
                continue

            # Normalize case — a user might type "fmpib6269486679" while
            # the email shows "FMPIB6269486679". Pure-digit UTRs are
            # unaffected by .upper().
            matches[utr.upper()] = amount

        return matches

    except Exception:
        logger.exception("upi: IMAP fetch failed")
        return {}

    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


async def verify_upi(deposit: Deposit) -> Optional[Union[dict, bool]]:
    """
    Verify a UPI deposit by matching its stored UTR (in deposit.tx_hash)
    against recent FamApp payment emails.

    Returns:
        None  -> UTR not found yet (keep it pending, retry later)
        False -> tx_hash isn't a structurally valid UTR
        dict  -> matched transfer details, including the amount that
                 was actually parsed from the email
    """
    utr = deposit.tx_hash

    if not valid_utr(utr):
        logger.error("verify_upi: deposit %s has invalid UTR: %r", deposit.id, utr)
        return False

    try:
        matches = await asyncio.to_thread(_fetch_famapp_matches)
    except Exception:
        logger.exception("verify_upi: unexpected error checking deposit %s", deposit.id)
        return None  # treat as transient, retry on next pass

    amount = matches.get(utr.upper())

    if amount is None:
        return None  # not found (yet) — stays pending

    return {
        "sender": "UPI",
        "receiver": UPI_ID,
        "amount": amount,
        "utr": utr,
    }


# =====================================================
# DATABASE HELPERS
# =====================================================

def tx_already_processed(tx_hash: str) -> bool:
    """
    Returns True if another completed deposit already
    used this transaction hash / UTR.
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
    to be structurally invalid (bad tx_hash/UTR, unknown network,
    etc.) rather than "not yet confirmed".
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
        None  -> deposit still pending (checker unavailable, or not
                 matched/indexed yet)
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

        network = deposit.network.upper() if deposit.network else ""

        # ── UPI path ──
        if network == UPI_NETWORK:
            if not isinstance(deposit.tx_hash, str) or not valid_utr(deposit.tx_hash):
                logger.error(
                    "Deposit %s has invalid UTR: %r",
                    deposit.id,
                    deposit.tx_hash,
                )
                _fail_deposit(
                    db, deposit.id,
                    f"UTR failed format check: {deposit.tx_hash!r}",
                )
                return False

            verification = await verify_upi(deposit)

        # ── Crypto (BEP20 / POLYGON) path ──
        else:
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

            if network not in CHAINS:
                logger.error(
                    "Unknown network '%s' for deposit %s",
                    network,
                    deposit.id,
                )
                _fail_deposit(db, deposit.id, f"unknown network {network!r}")
                return False

            chain = CHAINS[network]

            verification = await verify_transaction(chain, deposit.tx_hash)

        # ── Shared result handling ──

        if verification is None:
            # Not confirmed / matched yet — leave as pending, try again later.
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
# CHECK SINGLE TRANSACTION (crypto only)
# =====================================================

async def check_single_transaction(tx_hash: str, network: str) -> Optional[Union[dict, bool]]:
    """
    Helper that verifies one crypto transaction without the
    background loop. Not applicable to UPI — call verify_upi()
    directly (with a Deposit-like object) for that.

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
