"""Глобальный error-handler (регистрируется на Dispatcher)."""
import logging
from aiogram import Router
from aiogram.types import ErrorEvent
from app.loader import bot, dp
from app.middlewares import security
logger = logging.getLogger(__name__)


_BENIGN_ERROR_PATTERNS = (
    "message is not modified",          # repeated edit_text with same content
    "query is too old",                 # callback fired after bot restart
    "MESSAGE_NOT_MODIFIED",
)


@dp.errors()
async def on_error(event: ErrorEvent):
    """Catch-all: log full context, show neutral message to user."""
    update = event.update
    user_id = None
    chat_id = None
    if update.message:
        user_id = update.message.from_user.id if update.message.from_user else None
        chat_id = update.message.chat.id
    elif update.callback_query:
        user_id = update.callback_query.from_user.id if update.callback_query.from_user else None
        chat_id = update.callback_query.message.chat.id if update.callback_query.message else None

    err_text = str(event.exception)
    if any(p in err_text for p in _BENIGN_ERROR_PATTERNS):
        logger.info(f"BENIGN_ERROR user={security.mask_user_id(user_id)} msg={err_text!r}")
        return True

    logger.exception(
        f"UNHANDLED user={security.mask_user_id(user_id)} type={type(event.exception).__name__}"
    )
    if chat_id:
        try:
            await bot.send_message(
                chat_id,
                "<b>Произошла ошибка</b>\n<i>Попробуйте повторить действие позже.</i>",
            )
        except Exception:
            pass
    return True
