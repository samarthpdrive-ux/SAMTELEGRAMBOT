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
so the shared logic (duplicate check, amount check, credit_user) only
exists once.

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

Optional (config.py):
    DEPOSIT_AMOUNT_TOLERANCE  - Decimal-compatible value (default
                              "0.01"). How much a verified/received
                              amount is allowed to fall short of the
                              amount the user was asked to pay before
                              the deposit is treated as underpaid and
                              failed instead of credited.
    DEPOSIT_ALLOW_OVERPAY    - bool, default True. If True, a payment
                              higher than requested is credited at the
                              FULL amount actually received. If False,
                              the credited amount is capped at the
                              requested amount (the extra is logged,
                              not added to balance).
    DEPOSIT_MAX_CHECK_ATTEMPTS - int, default 5. A deposit that stays
                              unresolved (still "pending" — e.g. a
                              persistent Etherscan "NOTOK" error, a
                              tx_hash on the wrong network, or a UPI
                              reference that never shows up) is only
                              re-checked this many times. After the
                              limit is hit it is auto-marked "failed"
                              instead of being retried forever every
                              CHECK_INTERVAL seconds.

FIX NOTES (latest):
- **CRITICAL FIX**: `_extract_amount()` now prioritizes "Received/
  Credited/Payment of" patterns over generic ₹/Rs/INR amounts. FamApp
  emails contain account balance figures and other amounts that were
  being incorrectly extracted (e.g. balance=Rs 7.54 being picked up
  instead of received=Rs 1). The new RECEIVED_AMOUNT_RE specifically
  targets lines like "Received Rs.1", "Amount Received: ₹1",
  "credited with Rs.1", "payment of Rs.1" — the actual payment amount.
- **FIX**: `credit_user()` now accepts both `credited_amount` (actual
  verified amount from email, used for user.balance) and
  `requested_amount` (amount user was asked to pay, stored in
  deposit.amount for FamApp display).
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

from decimal import Decimal
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

# Validate API key at startup
if not API_KEY:
    logger.warning("ETHERSCAN_API_KEY is not set! Etherscan API calls will fail.")

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
# SETTINGS — AMOUNT VERIFICATION
# =====================================================

try:
    AMOUNT_TOLERANCE = Decimal(str(getattr(config, "DEPOSIT_AMOUNT_TOLERANCE", "0.01")))
except (InvalidOperation, ValueError):
    AMOUNT_TOLERANCE = Decimal("0.01")

ALLOW_OVERPAY = getattr(config, "DEPOSIT_ALLOW_OVERPAY", True)

try:
    MAX_CHECK_ATTEMPTS = int(getattr(config, "DEPOSIT_MAX_CHECK_ATTEMPTS", 5))
except (TypeError, ValueError):
    MAX_CHECK_ATTEMPTS = 5

_check_attempts: dict[int, int] = {}


def _record_pending_attempt(deposit_id: int) -> int:
    """Increment and return this deposit's consecutive 'still pending'
    check count."""
    count = _check_attempts.get(deposit_id, 0) + 1
    _check_attempts[deposit_id] = count
    return count


def _clear_pending_attempts(deposit_id: int) -> None:
    """Forget the attempt count once a deposit stops being pending."""
    _check_attempts.pop(deposit_id, None)


DELETE_FAILED_DEPOSITS = getattr(config, "DEPOSIT_DELETE_FAILED", True)


def _finalize_failed(db, deposit: Deposit, reason: str) -> None:
    """
    Single place where a deposit's failure is resolved.
    """
    deposit_id = deposit.id
    tx_hash = deposit.tx_hash

    if DELETE_FAILED_DEPOSITS:
        db.delete(deposit)
        db.commit()
        logger.warning(
            "Deposit %s deleted (failed: %s) | tx=%s",
            deposit_id, reason, tx_hash,
        )
    else:
        deposit.status = "failed"
        db.commit()
        logger.warning(
            "Deposit %s marked failed: %s | tx=%s",
            deposit_id, reason, tx_hash,
        )

    _clear_pending_attempts(deposit_id)


# =====================================================
# SETTINGS — UPI / IMAP
# =====================================================

IMAP_HOST = getattr(config, "IMAP_HOST", "imap.gmail.com")

IMAP_EMAIL = getattr(config, "IMAP_EMAIL", "")

IMAP_APP_PASSWORD = getattr(config, "IMAP_APP_PASSWORD", "")

FAMAPP_SENDER_EMAIL = getattr(config, "FAMAPP_SENDER_EMAIL", "")

UPI_ID = getattr(config, "UPI_ID", "")

IMAP_LOOKBACK_DAYS = getattr(config, "IMAP_LOOKBACK_DAYS", 1)

MAX_EMAILS_TO_SCAN = getattr(config, "UPI_MAX_EMAILS_TO_SCAN", 40)

# A UPI UTR (per NPCI) is always a 12-digit number.
UTR_RE = re.compile(r"^\d{12}$")

# App transaction IDs like "FMPIB6269486679"
TXN_ID_RE = re.compile(r"^[A-Za-z]{3,10}\d{6,15}$")

# Looks for a UTR/txn-id near a UTR/RRN/reference/txn-id label
UTR_LABEL_RE = re.compile(
    r"(?:UTR(?:\s*No\.?)?|UPI\s*Ref(?:erence)?(?:\s*No\.?)?|RRN|"
    r"Txn\s*Ref(?:erence)?|Transaction\s*ID|Txn(?:\s*ID)?)"
    r"[\s:\-]{0,10}([A-Za-z0-9]{8,20})",
    re.IGNORECASE,
)

# Fallback 1: any bare 12-digit number in the email
UTR_FALLBACK_RE = re.compile(r"\b(\d{12})\b")

# Fallback 2: any bare app-style txn id
TXN_ID_FALLBACK_RE = re.compile(r"\b([A-Za-z]{3,10}\d{6,15})\b")

# Generic amount pattern: "Rs. 500", "₹500.00", "INR 1,200"
AMOUNT_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

# CRITICAL FIX: Specific pattern for "Received/Credited/Payment" amount
# FamApp emails contain account balance, cashback, etc. — we need to
# specifically target the actual payment received amount.
# Common patterns in FamApp payment emails:
#   "Received Rs.500" / "Received: ₹500"
#   "Amount Received: ₹500"
#   "credited with Rs.500"
#   "payment of Rs.500"
#   "Paid: Rs.500"
RECEIVED_AMOUNT_RE = re.compile(
    r"(?:Received|Credited|Payment\s+of|Amount\s+Received|Paid)"
    r"[:\s]*"
    r"(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d{1,2})?)",
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
    "BEP20": Chain("BEP20", 56, BEP20_ADDRESS, USDT_BSC, 18),
    "POLYGON": Chain("POLYGON", 137, POLYGON_ADDRESS, USDT_POLYGON, 6),
}

# =====================================================
# HELPERS — CRYPTO
# =====================================================

def valid_hash(tx_hash: str) -> bool:
    """Validate an EVM transaction hash format."""
    if tx_hash is None:
        return False
    if not isinstance(tx_hash, str):
        return False
    return tx_hash.startswith("0x") and len(tx_hash) == 66


def raw_to_amount(value: int, decimals: int) -> Decimal:
    """Convert raw integer amount to Decimal."""
    return Decimal(value) / Decimal(10 ** decimals)


async def etherscan_receipt(chain: Chain, tx_hash: str) -> Optional[dict]:
    """Fetch transaction receipt from Etherscan V2 API."""
    params = {
        "chainid": int(chain.chainid),
        "module": "proxy",
        "action": "eth_getTransactionReceipt",
        "txhash": tx_hash,
        "apikey": API_KEY,
    }

    logger.debug(
        "[%s] Etherscan API request | chainid=%s | tx=%s | apikey=%s...",
        chain.name, params["chainid"], tx_hash,
        API_KEY[:8] if API_KEY else "EMPTY",
    )

    param_str = "&".join(f"{k}={v}" for k, v in params.items())
    full_url = f"{ETHERSCAN_URL}?{param_str}"
    logger.debug("[%s] Full URL (redacted apikey): %s", chain.name,
                 full_url.replace(str(API_KEY), "API_KEY_HIDDEN"))

    try:
        response = await asyncio.to_thread(
            requests.get, ETHERSCAN_URL, params=params,
            timeout=HTTP_TIMEOUT, headers={"Accept": "application/json"},
        )
        logger.debug("[%s] Response status=%s | url=%s", chain.name, response.status_code, response.url)
        response.raise_for_status()
        data = response.json()
        logger.debug("[%s] API response: %s", chain.name, data)
    except requests.exceptions.RequestException as e:
        logger.error("[%s] API Request Error for tx %s: %s", chain.name, tx_hash, e)
        return None
    except Exception as e:
        logger.error("[%s] API Error for tx %s: %s", chain.name, tx_hash, e)
        return None

    if data.get("status") == "0":
        result_str = data.get("result", "")
        message = data.get("message", "Unknown error")

        if "Missing chainid" in str(result_str) or "Missing chainid" in str(message):
            logger.error(
                "[%s] Etherscan says chainid is missing. Sent params: chainid=%s (type=%s) | tx=%s",
                chain.name, params["chainid"], type(params["chainid"]).__name__, tx_hash,
            )
            fallback_url = (
                f"{ETHERSCAN_URL}?chainid={chain.chainid}"
                f"&module=proxy&action=eth_getTransactionReceipt"
                f"&txhash={tx_hash}&apikey={API_KEY}"
            )
            logger.warning("[%s] Attempting fallback direct URL request...", chain.name)
            try:
                response2 = await asyncio.to_thread(
                    requests.get, fallback_url,
                    timeout=HTTP_TIMEOUT, headers={"Accept": "application/json"},
                )
                data2 = response2.json()
                logger.debug("[%s] Fallback response: %s", chain.name, data2)
                if data2.get("status") != "0":
                    logger.info("[%s] Fallback request succeeded!", chain.name)
                    data = data2
                    if data.get("result") and isinstance(data.get("result"), dict):
                        return data.get("result")
                    return None
                else:
                    logger.error("[%s] Fallback also failed: %s | message=%s",
                                 chain.name, data2.get("result"), data2.get("message"))
            except Exception as fallback_error:
                logger.error("[%s] Fallback request error: %s", chain.name, fallback_error)

        logger.error("[%s] API returned error: message=%s | result=%s", chain.name, message, result_str)
        return None

    result = data.get("result")
    if result is None:
        logger.info("[%s] No result for tx %s (pending)", chain.name, tx_hash)
        return None
    if isinstance(result, dict):
        return result
    logger.info("[%s] Unexpected result type for tx %s: %s", chain.name, tx_hash, type(result))
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
    """Search receipt logs for a valid USDT Transfer event directed to our wallet."""
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
        amount = raw_to_amount(amount_raw, chain.decimals)
        return {
            "sender": sender,
            "receiver": receiver,
            "amount": amount,
            "contract": contract,
        }
    return None


async def verify_transaction(chain: Chain, tx_hash: str) -> Optional[Union[dict, bool]]:
    """Verify a transaction using the Etherscan V2 API."""
    receipt = await etherscan_receipt(chain, tx_hash)
    if receipt is None:
        return None
    status = receipt.get("status")
    if status != "0x1":
        logger.info("[%s] %s failed", chain.name, tx_hash)
        return False
    transfer = parse_transfer(chain, receipt)
    if transfer is None:
        logger.info("[%s] %s has no valid USDT transfer", chain.name, tx_hash)
        return False
    transfer["receipt"] = receipt
    return transfer


# =====================================================
# HELPERS — UPI
# =====================================================

def valid_utr(utr: str) -> bool:
    """Validate a user-submitted UPI reference."""
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


def _extract_utr_match(text: str):
    """Returns regex Match object for UTR/reference, or None."""
    if not text:
        return None
    match = UTR_LABEL_RE.search(text)
    if match:
        return match
    match = UTR_FALLBACK_RE.search(text)
    if match:
        return match
    match = TXN_ID_FALLBACK_RE.search(text)
    if match:
        return match
    return None


def _extract_utr(text: str) -> Optional[str]:
    match = _extract_utr_match(text)
    return match.group(1) if match else None


def _extract_amount(text: str, near_pos: Optional[int] = None) -> Optional[Decimal]:
    """
    Find the ACTUAL payment received amount from a FamApp email.

    FamApp payment emails contain multiple currency figures:
    - Account balance (e.g. "your balance is Rs 7.54")
    - Cashback amounts (e.g. "get Rs 10 cashback")
    - The actual received payment (e.g. "Received Rs.1")

    This function prioritizes finding the "Received/Credited/Payment of"
    amount first, which is the actual payment. If that specific pattern
    isn't found, it falls back to proximity-based matching and then
    first-match.
    """
    if not text:
        return None

    def to_decimal(raw: str) -> Optional[Decimal]:
        cleaned = raw.replace(",", "")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None

    # PRIORITY 1: Look for explicit "Received/Credited/Payment of" amount
    # This is the ACTUAL amount paid, not balance/cashback/other figures
    received_matches = list(RECEIVED_AMOUNT_RE.finditer(text))
    if received_matches:
        # If near_pos is given, prefer the received amount closest to the UTR
        if near_pos is not None:
            best_match = min(received_matches, key=lambda m: abs(m.start(1) - near_pos))
            value = to_decimal(best_match.group(1))
            if value is not None:
                logger.debug(
                    "_extract_amount: found RECEIVED amount %s near UTR pos %s",
                    value, near_pos,
                )
                return value

        # Otherwise use the first received amount found
        value = to_decimal(received_matches[0].group(1))
        if value is not None:
            logger.debug(
                "_extract_amount: found RECEIVED amount %s (first match)",
                value,
            )
            return value

    # PRIORITY 2: Use proximity-based matching for all generic ₹/Rs/INR patterns
    matches = list(AMOUNT_RE.finditer(text))
    if not matches:
        logger.debug("_extract_amount: no amounts found in email text")
        return None

    if near_pos is not None:
        best_match = min(matches, key=lambda m: abs(m.start() - near_pos))
        value = to_decimal(best_match.group(1))
        if value is not None:
            logger.debug(
                "_extract_amount: found generic amount %s nearest to UTR pos %s",
                value, near_pos,
            )
            return value

    # PRIORITY 3: Fall back to first amount in email
    value = to_decimal(matches[0].group(1))
    logger.debug("_extract_amount: fallback to first generic amount %s", value)
    return value


def _fetch_famapp_matches() -> dict:
    """
    Blocking IMAP call. Connects to Gmail, scans recent emails from
    FamApp, and returns a dict of {utr: amount} for every payment
    notification found in the lookback window.
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
        recent_ids = email_ids[-MAX_EMAILS_TO_SCAN:]

        if not recent_ids:
            return {}

        # Batch fetch all emails in ONE IMAP round-trip
        ids_csv = b",".join(recent_ids)
        status, msg_data = mail.fetch(ids_csv, "(RFC822)")

        if status != "OK" or not msg_data:
            logger.error("upi: batch IMAP fetch failed: %s", status)
            return {}

        raw_emails = [item[1] for item in msg_data if isinstance(item, tuple)]

        for raw_email in raw_emails:
            msg = email.message_from_bytes(raw_email)
            subject = _decode_text(msg.get("Subject"))
            body = _get_body(msg)
            full_text = f"{subject}\n{body}"

            logger.debug("upi: scanning email | subject=%s | body_preview=%s...",
                         subject[:80], body[:200])

            utr_match = _extract_utr_match(full_text)
            if not utr_match:
                logger.debug("upi: no UTR/txn-id match in this email")
                continue

            utr = utr_match.group(1)

            # Extract amount using the UTR's position for proximity-based matching
            amount = _extract_amount(full_text, near_pos=utr_match.start(1))
            if amount is None:
                logger.warning(
                    "upi: found UTR %s but couldn't parse an amount from "
                    "the email — check RECEIVED_AMOUNT_RE/AMOUNT_RE against email format",
                    utr,
                )
                continue

            key = utr.upper()

            if key in matches and matches[key] != amount:
                logger.warning(
                    "upi: UTR %s matched more than one differing amount "
                    "(%s vs %s) across emails — keeping the first one seen",
                    key, matches[key], amount,
                )
                continue

            matches[key] = amount
            logger.info(
                "upi: matched UTR=%s amount=%s in email",
                key, amount,
            )

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


def _match_upi(deposit: Deposit, matches: dict) -> Optional[Union[dict, bool]]:
    """Pure lookup against an already-fetched {utr: amount} dict."""
    utr = deposit.tx_hash

    if not valid_utr(utr):
        logger.error("verify_upi: deposit %s has invalid UTR: %r", deposit.id, utr)
        return False

    amount = matches.get(utr.upper())

    if amount is None:
        return None  # not found (yet) — stays pending

    return {
        "sender": "UPI",
        "receiver": UPI_ID,
        "amount": amount,
        "utr": utr,
    }


async def verify_upi(deposit: Deposit) -> Optional[Union[dict, bool]]:
    """Verify a single UPI deposit by matching its stored UTR against
    recent FamApp payment emails."""
    try:
        matches = await asyncio.to_thread(_fetch_famapp_matches)
    except Exception:
        logger.exception("verify_upi: unexpected error checking deposit %s", deposit.id)
        return None
    return _match_upi(deposit, matches)


# =====================================================
# DATABASE HELPERS
# =====================================================

def tx_already_processed(tx_hash: str) -> bool:
    """Returns True if another completed deposit already used this tx hash / UTR."""
    db = SessionLocal()
    try:
        dep = (
            db.query(Deposit)
            .filter(Deposit.tx_hash == tx_hash, Deposit.status == "completed")
            .first()
        )
        return dep is not None
    finally:
        db.close()


def credit_user(
    db,
    deposit: Deposit,
    credited_amount: Decimal,
    requested_amount: Decimal,
) -> bool:
    """
    Credits the user's balance with `credited_amount` (actual verified
    amount from email/on-chain).

    IMPORTANT:
    - `user.balance` += `credited_amount` (actual verified amount from email)
    - `user.total_deposit` += `requested_amount` (what user was asked to pay)
    - `deposit.amount` preserved as `requested_amount` (for FamApp display)
    """

    user = (
        db.query(User)
        .filter(User.telegram_id == deposit.telegram_id)
        .first()
    )

    if user is None:
        logger.error("User %s not found", deposit.telegram_id)
        return False

    # Credit ACTUAL verified amount to user's balance
    user.balance += Decimal(str(credited_amount))

    # Track REQUESTED amount in total_deposit for proper accounting
    if hasattr(user, "total_deposit"):
        user.total_deposit += float(requested_amount)

    # Preserve deposit.amount as REQUESTED amount (do NOT overwrite)
    if deposit.amount is None or float(deposit.amount) == 0:
        deposit.amount = float(requested_amount)

    deposit.status = "completed"

    # Store actual received amount for auditing if model supports it
    if hasattr(deposit, 'received_amount'):
        deposit.received_amount = float(credited_amount)

    db.commit()

    logger.info(
        "Deposit credited | User=%s Requested=%s Credited=%s Tx=%s Balance=%s",
        user.telegram_id, requested_amount, credited_amount,
        deposit.tx_hash, user.balance,
    )

    return True


def mark_deposit_failed(deposit_id: int, reason: str) -> None:
    """Resolve a pending deposit as failed."""
    db = SessionLocal()
    try:
        dep = db.get(Deposit, deposit_id)
        if dep is None:
            return
        if dep.status != "pending":
            return
        _finalize_failed(db, dep, reason)
    except Exception:
        db.rollback()
        logger.exception("Could not auto-fail deposit %s", deposit_id)
    finally:
        db.close()


def _fail_deposit(db, deposit_id: int, reason: str) -> None:
    """Resolve a deposit as failed using an already-open session."""
    try:
        dep = db.get(Deposit, deposit_id)
        if dep and dep.status not in ("completed", "failed"):
            _finalize_failed(db, dep, reason)
    except Exception:
        db.rollback()
        logger.exception("Could not auto-fail deposit %s", deposit_id)
    finally:
        _clear_pending_attempts(deposit_id)


# =====================================================
# VERIFY ONE DEPOSIT
# =====================================================

async def verify_deposit(
    deposit_or_id: Union[Deposit, int],
    upi_matches: Optional[dict] = None,
) -> Optional[bool]:
    """
    Verify a deposit by Deposit object or deposit ID.

    Returns:
        True  -> deposit verified and credited successfully
        False -> deposit failed, invalid, or underpaid
        None  -> deposit still pending
    """

    db = SessionLocal()

    try:
        if isinstance(deposit_or_id, int):
            deposit_id = deposit_or_id
        else:
            deposit_id = deposit_or_id.id

        deposit = db.get(Deposit, deposit_id)

        if deposit is None:
            logger.error("Deposit %s not found", deposit_id)
            _clear_pending_attempts(deposit_id)
            return False

        if deposit.status in ("completed", "failed"):
            _clear_pending_attempts(deposit_id)
            return deposit.status == "completed"

        # Capture what the user was asked to pay
        try:
            requested_amount = Decimal(str(deposit.amount))
        except (InvalidOperation, TypeError):
            requested_amount = None

        network = deposit.network.upper() if deposit.network else ""

        # ── UPI path ──
        if network == UPI_NETWORK:
            if not isinstance(deposit.tx_hash, str) or not valid_utr(deposit.tx_hash):
                logger.error("Deposit %s has invalid UTR: %r", deposit.id, deposit.tx_hash)
                _fail_deposit(db, deposit.id, f"UTR failed format check: {deposit.tx_hash!r}")
                return False

            if upi_matches is not None:
                verification = _match_upi(deposit, upi_matches)
            else:
                verification = await verify_upi(deposit)

        # ── Crypto path ──
        else:
            if not isinstance(deposit.tx_hash, str):
                logger.error("Deposit %s has invalid tx_hash type: %s", deposit.id, type(deposit.tx_hash))
                _fail_deposit(db, deposit.id, f"tx_hash not a string (got {type(deposit.tx_hash).__name__})")
                return False

            if not valid_hash(deposit.tx_hash):
                logger.error("Deposit %s has invalid tx_hash: %s", deposit.id, deposit.tx_hash)
                _fail_deposit(db, deposit.id, f"tx_hash failed format check: {deposit.tx_hash!r}")
                return False

            if network not in CHAINS:
                logger.error("Unknown network '%s' for deposit %s", network, deposit.id)
                _fail_deposit(db, deposit.id, f"unknown network {network!r}")
                return False

            chain = CHAINS[network]
            verification = await verify_transaction(chain, deposit.tx_hash)

        # ── Shared result handling ──

        if verification is None:
            attempts = _record_pending_attempt(deposit.id)
            if attempts >= MAX_CHECK_ATTEMPTS:
                _finalize_failed(
                    db, deposit,
                    f"unresolved after {attempts} check attempts (network={network})",
                )
                return False
            logger.info("Deposit %s still pending (attempt %s/%s)", deposit.id, attempts, MAX_CHECK_ATTEMPTS)
            return None

        _clear_pending_attempts(deposit.id)

        if verification is False:
            _finalize_failed(db, deposit, "verification returned false")
            return False

        # ── Duplicate check ──
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
            _finalize_failed(db, deposit, f"duplicate tx {deposit.tx_hash}")
            return False

        # ── Amount cross-check ──
        received_amount = verification["amount"]

        if requested_amount is not None:
            if received_amount < requested_amount - AMOUNT_TOLERANCE:
                _finalize_failed(
                    db, deposit,
                    f"underpaid: requested={requested_amount} received={received_amount}",
                )
                return False

            if received_amount > requested_amount + AMOUNT_TOLERANCE:
                if ALLOW_OVERPAY:
                    logger.info(
                        "Deposit %s overpaid | requested=%s received=%s tx=%s — crediting full received amount",
                        deposit.id, requested_amount, received_amount, deposit.tx_hash,
                    )
                else:
                    logger.info(
                        "Deposit %s overpaid | requested=%s received=%s tx=%s — capping credit at requested amount",
                        deposit.id, requested_amount, received_amount, deposit.tx_hash,
                    )
                    received_amount = requested_amount
        else:
            logger.warning(
                "Deposit %s has no valid requested amount — crediting verified amount as-is (deposit.amount=%r)",
                deposit.id, deposit.amount,
            )

        # ── Credit user ──
        return credit_user(
            db, deposit,
            credited_amount=received_amount,
            requested_amount=requested_amount if requested_amount is not None else received_amount,
        )

    except Exception:
        db.rollback()
        logger.exception("Error verifying deposit %s", deposit_id if "deposit_id" in locals() else deposit_or_id)
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
        rows = (
            db.query(Deposit.id, Deposit.network)
            .filter(Deposit.status == "pending")
            .all()
        )
    finally:
        db.close()

    crypto_ids = [dep_id for dep_id, network in rows if not network or network.upper() != UPI_NETWORK]
    upi_ids = [dep_id for dep_id, network in rows if network and network.upper() == UPI_NETWORK]

    still_pending_ids = {dep_id for dep_id, _network in rows}
    for stale_id in list(_check_attempts.keys()):
        if stale_id not in still_pending_ids:
            _check_attempts.pop(stale_id, None)

    logger.info("Checking %s pending deposits (%s crypto, %s UPI)...", len(rows), len(crypto_ids), len(upi_ids))

    for deposit_id in crypto_ids:
        try:
            await verify_deposit(deposit_id)
        except Exception:
            logger.exception("Failed checking deposit %s", deposit_id)

    if upi_ids:
        try:
            upi_matches = await asyncio.to_thread(_fetch_famapp_matches)
        except Exception:
            logger.exception("UPI batch IMAP fetch failed")
            upi_matches = {}

        for deposit_id in upi_ids:
            try:
                await verify_deposit(deposit_id, upi_matches=upi_matches)
            except Exception:
                logger.exception("Failed checking UPI deposit %s", deposit_id)


# =====================================================
# CHECK SINGLE TRANSACTION (crypto only)
# =====================================================

async def check_single_transaction(tx_hash: str, network: str) -> Optional[Union[dict, bool]]:
    """Verify one crypto transaction without the background loop."""
    network = network.upper()
    if network not in CHAINS:
        return None
    return await verify_transaction(CHAINS[network], tx_hash)


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
            logger.exception("Deposit checker crashed")
        await asyncio.sleep(CHECK_INTERVAL)


# =====================================================
# STARTER
# =====================================================

def start_checker():
    """Creates the background task. Call once during bot startup."""
    return asyncio.create_task(deposit_checker_loop())
