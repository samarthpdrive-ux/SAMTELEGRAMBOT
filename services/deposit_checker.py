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
                              CHECK_INTERVAL seconds. This also stops
                              a stuck/mismatched tx_hash from
                              indefinitely occupying a check slot and
                              colliding with the next legitimate
                              deposit attempt.

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
- **FIX**: verify_deposit() now compares the amount actually verified
  on-chain / in the payment email against the amount the user was
  asked to pay when the deposit was created. If the verified amount
  is short (beyond a small tolerance), the deposit is marked "failed"
  instead of being silently credited. This is what previously let a
  smaller real payment (e.g. actual Rs 2) satisfy and fully credit a
  larger requested deposit (e.g. user typed 5).
- **FIX**: `_extract_amount()` no longer blindly takes the first
  ₹/Rs/INR figure anywhere in the email. It now collects every amount
  match together with its text position and prefers whichever one
  sits closest to the matched UTR/reference-id text, since payment
  emails often contain unrelated amounts elsewhere (cashback, minimum
  balance, previous balance, etc.) above the actual "amount received"
  line, which was being picked up by mistake.
- **FIX**: a deposit that returns "still pending" (None) is now only
  retried DEPOSIT_MAX_CHECK_ATTEMPTS times (default 5). Previously a
  deposit that could never resolve on its own — e.g. an Etherscan API
  error like "NOTOK" repeating every check, or a tx_hash submitted for
  the wrong chain — stayed "pending" and got re-checked forever, every
  CHECK_INTERVAL seconds, without ever being cleared out. Attempt
  counts are tracked in-memory per deposit id and reset whenever a
  deposit resolves (credited/failed) or stops being pending.
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
# SETTINGS — AMOUNT VERIFICATION
# =====================================================

# How much a verified/received amount may fall short of the amount
# requested at deposit-creation time before we treat it as underpaid
# and fail the deposit instead of crediting it. Kept small to absorb
# only float/Decimal rounding noise, NOT genuine underpayment.
try:
    AMOUNT_TOLERANCE = Decimal(str(getattr(config, "DEPOSIT_AMOUNT_TOLERANCE", "0.01")))
except (InvalidOperation, ValueError):
    AMOUNT_TOLERANCE = Decimal("0.01")

# If True (default), an overpayment credits the FULL amount actually
# received. If False, credited amount is capped at what was requested.
ALLOW_OVERPAY = getattr(config, "DEPOSIT_ALLOW_OVERPAY", True)

# How many times a still-unresolved ("pending") deposit is re-checked
# before it's auto-failed instead of being retried forever. Covers
# both a persistent verification-source error (e.g. Etherscan NOTOK)
# and a tx_hash/UTR that simply never matches anything.
try:
    MAX_CHECK_ATTEMPTS = int(getattr(config, "DEPOSIT_MAX_CHECK_ATTEMPTS", 5))
except (TypeError, ValueError):
    MAX_CHECK_ATTEMPTS = 5

# In-memory per-deposit attempt counter. Intentionally not persisted:
# a bot restart resetting the count just gives a deposit a fresh set
# of attempts, which is harmless (worst case a few extra checks) —
# far simpler than a DB migration for something this low-stakes. Keyed
# by deposit id, pruned whenever a deposit stops being pending.
_check_attempts: dict[int, int] = {}


def _record_pending_attempt(deposit_id: int) -> int:
    """Increment and return this deposit's consecutive 'still pending'
    check count."""
    count = _check_attempts.get(deposit_id, 0) + 1
    _check_attempts[deposit_id] = count
    return count


def _clear_pending_attempts(deposit_id: int) -> None:
    """Forget the attempt count once a deposit stops being pending
    (credited, failed, or no longer exists)."""
    _check_attempts.pop(deposit_id, None)


# If True (default), a deposit that fails verification (bad
# format, underpaid, duplicate tx, unresolved after
# MAX_CHECK_ATTEMPTS, etc.) is DELETED from the database outright
# instead of being kept around with status="failed". Either way it
# will never be re-checked again — check_pending_deposits() only ever
# queries status == "pending" — but deleting also means it stops
# showing up in any admin/reporting query and its tx_hash/UTR becomes
# reusable for a future deposit attempt.
#
# Set config.DEPOSIT_DELETE_FAILED = False if you'd rather keep failed
# rows around (e.g. for support/dispute lookups) — they'll still be
# fully inert, just not physically removed.
DELETE_FAILED_DEPOSITS = getattr(config, "DEPOSIT_DELETE_FAILED", True)


def _finalize_failed(db, deposit: Deposit, reason: str) -> None:
    """
    Single place where a deposit's failure is resolved. Either deletes
    the row (default) or marks it status="failed" and leaves it,
    depending on DELETE_FAILED_DEPOSITS. Always clears its retry-
    attempt counter. Caller is responsible for everything else
    (returning False, etc.) — this only touches the DB row + counter.
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

# Hard cap on how many recent FamApp emails get fetched+parsed per
# check. This is what actually keeps checks fast — SINCE alone doesn't
# bound cost if the mailbox has a lot of FamApp mail in that window.
# We only ever care about the most recent ones anyway (a deposit is
# either matched within minutes of being paid, or it isn't yet).
MAX_EMAILS_TO_SCAN = getattr(config, "UPI_MAX_EMAILS_TO_SCAN", 40)

# A UPI UTR (per NPCI) is always a 12-digit number. Kept in sync with
# handlers/deposit.py's UTR_RE.
UTR_RE = re.compile(r"^\d{12}$")

# Some apps (FamApp and others) don't surface the bank UTR to the user
# at all — instead they show their own transaction ID, e.g.
# "FMPIB6269486679" (a few letters, then digits). Accept that shape too.
TXN_ID_RE = re.compile(r"^[A-Za-z]{3,10}\d{6,15}$")

# Looks for a UTR/txn-id near a UTR/RRN/reference/txn-id label.
# Captures either a 12-digit UTR or a letters+digits app txn id.
#
# IMPORTANT: the separator between the label and the value must be
# restricted to actual separator characters (whitespace / colon /
# dash) — NOT "\D" (any non-digit). \D also matches letters, so it
# was previously eating the "FMPIB" prefix off of ids like
# "FMPIB6269486679" (confirmed against a real FamApp email: "...with
# transaction id FMPIB6269486679.") before the capture group ever got
# a chance to see it.
UTR_LABEL_RE = re.compile(
    r"(?:UTR(?:\s*No\.?)?|UPI\s*Ref(?:erence)?(?:\s*No\.?)?|RRN|"
    r"Txn\s*Ref(?:erence)?|Transaction\s*ID|Txn(?:\s*ID)?)"
    r"[\s:\-]{0,10}([A-Za-z0-9]{8,20})",
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

    # NOTE: "status": "0" only appears on "account"/"log"-style
    # Etherscan endpoints. The "proxy" module used here
    # (eth_getTransactionReceipt) never sets a top-level "status" —
    # on failure it instead returns a JSON-RPC "error" object with no
    # "result" key. The check below was previously dead code for this
    # endpoint, so an invalid API key / rate limit / bad param just
    # looked identical to "tx not indexed yet", with nothing in the
    # logs explaining why.
    if data.get("status") == "0":
        logger.error(
            "[%s] API returned error for tx %s: %s",
            chain.name,
            tx_hash,
            data.get("message", "Unknown error"),
        )
        return None

    if "error" in data:
        # FIX: this is the actual error shape the proxy module
        # returns. Previously fell through to "result is None" with
        # zero indication of what went wrong.
        logger.error(
            "[%s] proxy API error for tx %s: %s",
            chain.name,
            tx_hash,
            data.get("error"),
        )
        return None

    result = data.get("result")

    if result is None:
        # A bare {"result": null} is the normal, expected shape while
        # the tx just hasn't been indexed yet — genuinely "still
        # pending", not an error.
        logger.info(
            "[%s] tx %s not yet indexed by Etherscan (result=null)",
            chain.name,
            tx_hash,
        )
        return None

    if isinstance(result, dict):
        return result

    logger.error(
        "[%s] unexpected result shape for tx %s: %r",
        chain.name,
        tx_hash,
        result,
    )
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


def _extract_utr_match(text: str):
    """
    Same search order as _extract_utr(), but returns the regex Match
    object (not just the captured string) so callers can also see
    *where* in the text the UTR/reference was found. Returns None if
    nothing matched.
    """
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
    Find an amount like "Rs. 500", "₹500.00", "INR 1,200" in `text`.

    FIX: a payment-notification email frequently contains more than
    one currency figure (e.g. "your account balance is Rs 4,500" or
    "get Rs 10 cashback" ABOVE the actual "Rs 2 received" line). Only
    taking the *first* match risked picking up the wrong number.

    If `near_pos` (a character offset into `text`, typically where the
    UTR/reference id was found) is given, this collects every amount
    match and returns whichever one is textually closest to that
    position — on the assumption that the real "amount received" line
    sits right next to the transaction reference in these emails.
    Falls back to the first match if `near_pos` isn't given or nothing
    is close.
    """
    if not text:
        return None

    matches = list(AMOUNT_RE.finditer(text))
    if not matches:
        return None

    def to_decimal(raw: str) -> Optional[Decimal]:
        cleaned = raw.replace(",", "")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None

    if near_pos is None:
        return to_decimal(matches[0].group(1))

    best_match = min(matches, key=lambda m: abs(m.start() - near_pos))
    value = to_decimal(best_match.group(1))

    if value is None:
        # extremely unlikely (regex already constrains the shape),
        # but fall back defensively to the first match.
        return to_decimal(matches[0].group(1))

    return value


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

        # FIX: this is the single most useful diagnostic line in this
        # whole module. If FAMAPP_SENDER_EMAIL (config.py) doesn't
        # exactly match the real "From:" address on payment emails,
        # this search silently returns 0 results forever — every UPI
        # deposit sits "pending" indefinitely with no error anywhere,
        # because "found nothing" and "found nothing yet" look
        # identical. Logging the count on every check makes a
        # persistent 0 obvious instead of invisible.
        logger.info(
            "upi: IMAP search for FROM \"%s\" SINCE %s found %s email(s)",
            FAMAPP_SENDER_EMAIL,
            since_date,
            len(email_ids),
        )

        # Only look at the most recent emails. Sequence numbers returned
        # by search() are ascending (oldest first), so the tail of the
        # list is the newest mail — which is all that matters for
        # catching a deposit made minutes ago.
        recent_ids = email_ids[-MAX_EMAILS_TO_SCAN:]

        if not recent_ids:
            # FIX: if this fires every single check, FAMAPP_SENDER_EMAIL
            # is almost certainly wrong. Open a real FamApp payment
            # email in Gmail -> "Show original" -> copy the exact
            # From: address into config.py.
            logger.warning(
                "upi: 0 emails matched FROM \"%s\" in the last %s day(s) — "
                "if this keeps happening, FAMAPP_SENDER_EMAIL in "
                "config.py is likely wrong. Open a real FamApp payment "
                "email -> 'Show original' -> copy the exact From address.",
                FAMAPP_SENDER_EMAIL,
                IMAP_LOOKBACK_DAYS,
            )
            return {}

        # Fetch all of them in ONE IMAP round-trip instead of one
        # request per email — this is what was making checks slow
        # when the mailbox had many FamApp emails.
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

            utr_match = _extract_utr_match(full_text)
            if not utr_match:
                continue

            utr = utr_match.group(1)

            # FIX: pass the UTR's position so _extract_amount prefers
            # the amount figure nearest to it, instead of always
            # grabbing the first ₹/Rs/INR figure in the whole email.
            amount = _extract_amount(full_text, near_pos=utr_match.start(1))
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
            key = utr.upper()

            if key in matches and matches[key] != amount:
                logger.warning(
                    "upi: UTR %s matched more than one differing amount "
                    "(%s vs %s) across emails — keeping the first one seen",
                    key, matches[key], amount,
                )
                continue

            matches[key] = amount

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
    """
    Pure lookup against an already-fetched {utr: amount} dict — does no
    IMAP I/O itself. Used both by verify_upi() (single-deposit path)
    and the batched background-loop path so a mailbox fetch is never
    repeated per deposit.
    """
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
    """
    Verify a single UPI deposit by matching its stored UTR (in
    deposit.tx_hash) against recent FamApp payment emails. Does its
    own IMAP fetch — fine for a one-off check (e.g. right after the
    user submits their UTR), but do NOT call this per-deposit in a
    loop over many pending deposits; use check_pending_deposits()
    instead, which fetches the mailbox once and matches every pending
    UPI deposit against it.

    Returns:
        None  -> UTR not found yet (keep it pending, retry later)
        False -> tx_hash isn't a structurally valid UTR
        dict  -> matched transfer details, including the amount that
                 was actually parsed from the email
    """
    try:
        matches = await asyncio.to_thread(_fetch_famapp_matches)
    except Exception:
        logger.exception("verify_upi: unexpected error checking deposit %s", deposit.id)
        return None  # treat as transient, retry on next pass

    return _match_upi(deposit, matches)


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
    """Credits the user's balance with `amount` (already
    tolerance/overpay-adjusted by the caller)."""

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
    Resolve a pending deposit as failed (deleted, or marked
    status="failed" depending on DELETE_FAILED_DEPOSITS) so the
    background loop stops retrying it and spamming logs. Used
    whenever a deposit is found to be structurally invalid (bad
    tx_hash/UTR, unknown network, underpaid, etc.) rather than
    "not yet confirmed".
    """

    db = SessionLocal()

    try:
        dep = db.get(Deposit, deposit_id)

        if dep is None:
            return

        if dep.status != "pending":
            # Already resolved by someone else — don't clobber it.
            return

        _finalize_failed(db, dep, reason)

    except Exception:
        db.rollback()
        logger.exception(
            "Could not auto-fail deposit %s",
            deposit_id,
        )

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
        # Any failure path — structural or attempt-limit — means this
        # deposit is done being checked, so its retry count is no
        # longer relevant.
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

    upi_matches: optional pre-fetched {utr: amount} dict (see
    check_pending_deposits()). Pass this when checking many UPI
    deposits in one pass so the mailbox is only fetched once. Leave
    as None for one-off checks (e.g. right after the user submits
    their UTR) — a fresh fetch will be done automatically.

    Returns:
        True  -> deposit verified and credited successfully
        False -> deposit failed, invalid, or underpaid
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
            _clear_pending_attempts(deposit_id)
            return False

        if deposit.status in ("completed", "failed"):
            # Already resolved (e.g. by a concurrent check) — nothing to do.
            _clear_pending_attempts(deposit_id)
            return deposit.status == "completed"

        # FIX: capture what the user was actually asked to pay BEFORE
        # anything below has a chance to touch deposit.amount. This is
        # compared against the verified amount further down instead of
        # trusting the verified amount unconditionally.
        try:
            requested_amount = Decimal(str(deposit.amount))
        except (InvalidOperation, TypeError):
            requested_amount = None

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

            if upi_matches is not None:
                verification = _match_upi(deposit, upi_matches)
            else:
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
            # Not confirmed / matched yet.
            #
            # FIX: cap how many times a deposit is allowed to sit
            # unresolved. Without this, a deposit that can never
            # resolve on its own — e.g. the wrong tx_hash for this
            # network, or a persistent verification-source error
            # (Etherscan "NOTOK", IMAP outage) — stays "pending"
            # forever and gets re-checked (and re-logged) every
            # CHECK_INTERVAL seconds indefinitely, occupying that
            # check slot and cluttering logs/DB with something that
            # will never confirm.
            attempts = _record_pending_attempt(deposit.id)

            if attempts >= MAX_CHECK_ATTEMPTS:
                _finalize_failed(
                    db, deposit,
                    f"unresolved after {attempts} check attempts "
                    f"(network={network})",
                )
                return False

            logger.info(
                "Deposit %s still pending (attempt %s/%s)",
                deposit.id,
                attempts,
                MAX_CHECK_ATTEMPTS,
            )
            return None

        # Resolved one way or another below — attempt count no longer needed.
        _clear_pending_attempts(deposit.id)

        if verification is False:
            _finalize_failed(db, deposit, "verification returned false")
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
            _finalize_failed(db, deposit, f"duplicate tx {deposit.tx_hash}")
            return False

        # ── FIX: amount cross-check ──
        # Compare what was actually received/verified against what the
        # user was asked to pay. Without this, ANY confirmed payment
        # matching the tx_hash/UTR — regardless of amount — was being
        # accepted, which is what let a smaller real payment (e.g. Rs 2)
        # satisfy and get treated the same as a larger requested deposit
        # (e.g. user typed 5).
        received_amount = verification["amount"]

        if requested_amount is not None:
            if received_amount < requested_amount - AMOUNT_TOLERANCE:
                _finalize_failed(
                    db, deposit,
                    f"underpaid: requested={requested_amount} "
                    f"received={received_amount}",
                )
                return False

            if received_amount > requested_amount + AMOUNT_TOLERANCE:
                if ALLOW_OVERPAY:
                    logger.info(
                        "Deposit %s overpaid | requested=%s received=%s tx=%s "
                        "— crediting full received amount",
                        deposit.id,
                        requested_amount,
                        received_amount,
                        deposit.tx_hash,
                    )
                else:
                    logger.info(
                        "Deposit %s overpaid | requested=%s received=%s tx=%s "
                        "— capping credit at requested amount",
                        deposit.id,
                        requested_amount,
                        received_amount,
                        deposit.tx_hash,
                    )
                    received_amount = requested_amount
        else:
            logger.warning(
                "Deposit %s has no valid requested amount to compare against "
                "(deposit.amount=%r) — crediting verified amount as-is",
                deposit.id,
                deposit.amount,
            )

        # ── Credit the user ──
        return credit_user(db, deposit, received_amount)

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
    """
    Scan all pending deposits and verify them.

    Crypto deposits are checked one Etherscan call each (unavoidable,
    each has a distinct tx_hash). UPI deposits are checked with ONE
    shared mailbox fetch for the whole batch, then matched in memory
    — NOT one mailbox login per deposit, which is what previously
    made UPI checks lag by minutes whenever more than a couple of UPI
    deposits were pending at once.
    """

    db = SessionLocal()

    try:
        rows = (
            db.query(Deposit.id, Deposit.network)
            .filter(Deposit.status == "pending")
            .all()
        )

    finally:
        db.close()

    crypto_ids = [
        dep_id for dep_id, network in rows
        if not network or network.upper() != UPI_NETWORK
    ]

    upi_ids = [
        dep_id for dep_id, network in rows
        if network and network.upper() == UPI_NETWORK
    ]

    # Prune attempt counters for any deposit no longer pending (e.g.
    # resolved manually by an admin, outside this module) so the
    # in-memory dict can't grow unbounded over a long-running process.
    still_pending_ids = {dep_id for dep_id, _network in rows}
    for stale_id in list(_check_attempts.keys()):
        if stale_id not in still_pending_ids:
            _check_attempts.pop(stale_id, None)

    logger.info(
        "Checking %s pending deposits (%s crypto, %s UPI)...",
        len(rows),
        len(crypto_ids),
        len(upi_ids),
    )

    for deposit_id in crypto_ids:
        try:
            await verify_deposit(deposit_id)

        except Exception:
            logger.exception(
                "Failed checking deposit %s",
                deposit_id,
            )

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
                logger.exception(
                    "Failed checking UPI deposit %s",
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
