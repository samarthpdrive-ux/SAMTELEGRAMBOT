# Tracks the most recent "menu" message per user (chat_id, message_id),
# stored in the database so it survives bot restarts (e.g. Render's free
# tier spinning the service down between requests).
#
# Why this exists:
# Callback buttons can edit the message they're attached to (edit_text),
# so tapping a button never creates a duplicate. But a text COMMAND like
# /admin or /start has no existing bot message to edit — it can only send
# a new one. replace_menu() fixes this by deleting the last tracked menu
# message for that user before sending a fresh one, so only one menu is
# ever visible, even across restarts.
#
# IMPORTANT: every DB call here is wrapped in try/except. This feature is
# a "nice to have" (removes duplicate menus) — it must never be able to
# break /start or /admin themselves. If the menu_state table is missing,
# the DB connection hiccups, or anything else goes wrong, we log it and
# fall back to just sending the menu normally (old behavior, no dedupe).

import logging

from database import SessionLocal
from models.menu_state import MenuState

logger = logging.getLogger(__name__)


def track(telegram_id: int, chat_id: int, message_id: int):
    db = SessionLocal()
    try:
        state = (
            db.query(MenuState)
            .filter(MenuState.telegram_id == telegram_id)
            .first()
        )

        if state:
            state.chat_id = chat_id
            state.message_id = message_id
        else:
            state = MenuState(
                telegram_id=telegram_id,
                chat_id=chat_id,
                message_id=message_id
            )
            db.add(state)

        db.commit()
    except Exception as e:
        logger.warning(f"menu_tracker.track failed (non-fatal): {e}")
        db.rollback()
    finally:
        db.close()


def get_tracked(telegram_id: int):
    db = SessionLocal()
    try:
        state = (
            db.query(MenuState)
            .filter(MenuState.telegram_id == telegram_id)
            .first()
        )

        if not state:
            return None

        return (state.chat_id, state.message_id)
    except Exception as e:
        logger.warning(f"menu_tracker.get_tracked failed (non-fatal): {e}")
        return None
    finally:
        db.close()


async def replace_menu(
        bot,
        chat_id: int,
        telegram_id: int,
        text: str,
        keyboard,
        parse_mode: str = None
):
    old = get_tracked(telegram_id)

    if old:
        old_chat_id, old_message_id = old
        try:
            await bot.delete_message(
                chat_id=old_chat_id,
                message_id=old_message_id
            )
        except Exception:
            # Already deleted, too old (>48h), or no permission — safe to ignore
            pass

    # send_message itself is NOT wrapped — if this fails, the user should
    # see the real Telegram API error, not have it silently swallowed.
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        parse_mode=parse_mode
    )

    track(telegram_id, chat_id, msg.message_id)

    return msg
