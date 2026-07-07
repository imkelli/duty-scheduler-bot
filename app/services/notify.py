"""Уведомления администратору и пользователям."""
import logging
from datetime import datetime as _dt
from app.db import database
from app.handlers.helpers import (
    SEP, esc,
)
from app.loader import bot
from app.middlewares import security
from config import ADMIN_ID
logger = logging.getLogger(__name__)


def _now_local() -> str:
    return _dt.now().strftime("%d.%m.%Y %H:%M")


async def _notify_admin(text: str, reply_markup=None):
    """Send text to admin chat; log a warning if delivery fails."""
    if not ADMIN_ID:
        logger.warning("ADMIN_ID is not set; admin notification skipped")
        return
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Admin notification failed: {type(e).__name__}: {e}")


async def _notify_admin_link(eng: dict, user, *, source: str):
    """source ∈ {'start', 'addme'} — purely for logs."""
    if user.id == ADMIN_ID:
        return  # don't spam admin about themselves
    tag = user.username and f"@{user.username}" or "—"
    text = (
        "<b>Новая привязка</b>\n"
        "\n"
        f"Имя: {esc(eng['full_name'])}\n"
        f"Telegram: <code>{esc(tag)}</code>\n"
        f"User ID: <code>{user.id}</code>\n"
        f"Время: <code>{_now_local()}</code>\n"
        "\n"
        f"{SEP}\n"
        "<i>Пользователь зарегистрирован в системе</i>"
    )
    await _notify_admin(text)
    logger.info(f"NOTIFY_LINK source={source} engineer_id={eng['id']} user={security.mask_user_id(user.id)}")


async def _notify_admin_unlink(eng: dict, user):
    if user.id == ADMIN_ID:
        return
    tag = user.username and f"@{user.username}" or (eng.get("telegram_tag") or "—")
    text = (
        "<b>Отвязка аккаунта</b>\n"
        "\n"
        f"Имя: {esc(eng['full_name'])}\n"
        f"Telegram: <code>{esc(tag)}</code>\n"
        f"Время: <code>{_now_local()}</code>"
    )
    await _notify_admin(text)
    logger.info(f"NOTIFY_UNLINK engineer_id={eng['id']} user={security.mask_user_id(user.id)}")


async def _notify_admin_failed_link(user):
    if user.id == ADMIN_ID:
        return
    tag = user.username and f"@{user.username}" or "—"
    text = (
        "<b>Неудачная привязка</b>\n"
        "\n"
        f"Telegram: <code>{esc(tag)}</code>\n"
        f"User ID: <code>{user.id}</code>\n"
        f"Время: <code>{_now_local()}</code>\n"
        "\n"
        f"{SEP}\n"
        "<i>Тег не найден в базе Phones</i>"
    )
    await _notify_admin(text)
    logger.info(f"NOTIFY_FAILED_LINK user={security.mask_user_id(user.id)} tag={tag!r}")


async def _notify_user_safe(user_id: int, text: str, reply_markup=None):
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
        return True
    except Exception:
        logger.warning(f"Could not notify user {security.mask_user_id(user_id)}")
        return False


async def _refresh_admin_tag():
    """Cache the admin's Telegram tag (from the DB record by ADMIN_ID)."""
    try:
        rec = await database.get_engineer_by_user_id(ADMIN_ID)
        security.set_admin_tag(rec.get("telegram_tag") if rec else None)
    except Exception:
        logger.exception("Failed to refresh admin tag")
        security.set_admin_tag(None)
