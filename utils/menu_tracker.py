# Tracks the most recent "menu" message per user (chat_id, message_id).
#
# Why this exists:
# Callback buttons can edit the message they're attached to (edit_text),
# so tapping a button never creates a duplicate. But a text COMMAND like
# /admin or /start has no existing bot message to edit — it can only send
# a new one. That's what caused the old menu to stay stuck on screen while
# a new one appeared below it.
#
# replace_menu() fixes this: before sending a fresh menu, it deletes the
# last tracked one for that user, so only one menu is ever visible.
#
# Note: this is in-memory, so it resets on bot restart. That's fine — the
# only effect is that immediately after a restart, one old menu message
# might not get auto-deleted the first time. It self-corrects after that.

active_menu_message = {}  # telegram_id -> (chat_id, message_id)


def track(telegram_id: int, chat_id: int, message_id: int):
    active_menu_message[telegram_id] = (chat_id, message_id)


async def replace_menu(
        bot,
        chat_id: int,
        telegram_id: int,
        text: str,
        keyboard,
        parse_mode: str = None
):
    old = active_menu_message.get(telegram_id)

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

    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        parse_mode=parse_mode
    )

    track(telegram_id, chat_id, msg.message_id)

    return msg
