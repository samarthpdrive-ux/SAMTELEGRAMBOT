"""
database.py

Engine / session configuration for TiDB Cloud (MySQL wire protocol,
via PyMySQL) using SQLAlchemy 2.x.

Notes on the choices below, since they're easy to get subtly wrong
against TiDB specifically:

- SQLAlchemy 2.0 has no `future=` flag anymore — "future style" is
  the only style, so there's nothing to opt into. (Passing future=
  to create_engine() on 2.0 is not needed and is not used here.)

- `expire_on_commit=False` is required for this project: handlers
  read attributes off ORM objects (order.id, user.balance, etc.)
  *after* db.commit() inside the same request, often after the
  session has already been closed in a `finally:` block. With the
  default `expire_on_commit=True`, that first attribute access after
  commit would silently try to re-run a SELECT on a closed session
  and raise.

- TiDB defaults to pessimistic transactions since TiDB 5.0, but that
  is a *cluster-level* default (`tidb_txn_mode`) that can be
  overridden per-session. `with_for_update()` (SELECT ... FOR UPDATE)
  only actually blocks other transactions under pessimistic mode. If
  this cluster (or a future one this bot is pointed at) ever has
  `tidb_txn_mode = 'optimistic'` as its default, `with_for_update()`
  would silently become a no-op instead of a real lock — no error,
  just no locking. We remove that ambiguity entirely by setting
  `tidb_txn_mode = 'pessimistic'` explicitly on every new connection
  in `_set_tidb_session_options` below, instead of trusting the
  cluster default.

- Even with real pessimistic locks, TiDB can still raise a write
  conflict (error 9007, "Write conflict") when two pessimistic
  transactions genuinely collide, or a "Table 'x' doesn't exist" style
  transient error after a schema change propagates across the
  cluster. The right response to error 9007 specifically is "retry the
  whole transaction" — it is not a bug, it's TiDB telling you to try
  again. `run_with_retry()` / the `@retry_on_write_conflict` decorator
  below exist for exactly this.
"""

from __future__ import annotations

import functools
import logging
import time
from contextlib import contextmanager
from urllib.parse import quote_plus

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker, declarative_base

from config import (
    MYSQL_USER,
    MYSQL_PASSWORD,
    MYSQL_HOST,
    MYSQL_PORT,
    MYSQL_DB,
    MYSQL_SSL_CA,
)

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------
# CONNECTION
# -----------------------------------------------------------------

encoded_password = quote_plus(MYSQL_PASSWORD)

DATABASE_URL = (
    f"mysql+pymysql://"
    f"{MYSQL_USER}:"
    f"{encoded_password}@"
    f"{MYSQL_HOST}:"
    f"{MYSQL_PORT}/"
    f"{MYSQL_DB}"
)

logger.info(
    "Connecting to TiDB Cloud | host=%s port=%s db=%s user=%s",
    MYSQL_HOST, MYSQL_PORT, MYSQL_DB, MYSQL_USER,
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,     # discard dead connections instead of erroring
    pool_recycle=3600,      # recycle before TiDB/any LB idle-closes it
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_reset_on_return="rollback",
    echo=False,
    connect_args={
        "ssl_ca": MYSQL_SSL_CA,
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    },
)


@event.listens_for(engine, "connect")
def _set_tidb_session_options(dbapi_connection, connection_record):
    """
    Runs once per new physical connection (not per checkout — this is
    the DBAPI 'connect' event, not 'checkout'). Forces pessimistic
    transaction mode explicitly rather than trusting the cluster
    default, so with_for_update() is guaranteed to take a real lock.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION tidb_txn_mode = 'pessimistic'")
    except Exception:
        # If this is ever pointed at plain MySQL (e.g. local dev),
        # tidb_txn_mode won't exist — don't crash startup over it.
        logger.warning(
            "Could not set tidb_txn_mode=pessimistic (not TiDB?) — "
            "continuing with engine defaults.",
            exc_info=True,
        )
    finally:
        cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -----------------------------------------------------------------
# TRANSACTION HELPERS
# -----------------------------------------------------------------

@contextmanager
def transaction():
    """
    One session, one transaction: commits on clean exit, rolls back
    and re-raises on any exception, always closes.

        with transaction() as db:
            user = db.query(User).filter(...).with_for_update().first()
            ...
            db.add(order)
        # committed here, or rolled back if anything raised

    Use this (or run_with_retry() below, which wraps this) for any
    write path that touches more than one row/table and needs all of
    it to succeed or none of it to — balance deduction + stock
    deduction + order insert must land together.
    """
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# TiDB write-conflict error code. Seen as OperationalError with this
# code embedded, e.g.:
#   (1105, 'Information schema is changed...')  -> schema race, retry
#   (9007, 'Write conflict...')                  -> txn race, retry
#   (1213, 'Deadlock found...')                  -> two txns locking
#                                                    rows in opposite
#                                                    order, retry
_RETRYABLE_TIDB_ERROR_CODES = (9007, 1105, 8022, 8028, 1213)


def _is_retryable(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ()) if orig is not None else ()
    if not args:
        return False
    code = args[0]
    return code in _RETRYABLE_TIDB_ERROR_CODES


def retry_on_write_conflict(max_attempts: int = 3, base_delay: float = 0.05):
    """
    Decorator for a function whose ENTIRE body is one transaction
    attempt (typically one that opens its own `with transaction():`
    block). On a retryable TiDB error, the function is called again
    from scratch, up to `max_attempts` times, with a short backoff.

    Do NOT wrap a function that has already committed some of its
    work and only fails partway through outside a transaction — this
    only makes sense around a single all-or-nothing attempt.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except OperationalError as exc:
                    if not _is_retryable(exc) or attempt == max_attempts:
                        raise
                    last_exc = exc
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "Retryable TiDB error on attempt %s/%s for %s: %s "
                        "— retrying in %.2fs",
                        attempt, max_attempts, func.__name__, exc, delay,
                    )
                    time.sleep(delay)
            # Unreachable, but keeps type checkers happy.
            if last_exc:
                raise last_exc
        return wrapper
    return decorator
