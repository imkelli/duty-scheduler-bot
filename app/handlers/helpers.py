"""Общие хелперы хэндлеров: доступ, экранирование, гашение сообщений, сбросы."""
import logging
import asyncio
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardRemove
from typing import Optional
from app import keyboards
from app.db import database
from app.loader import bot
from app.middlewares import security
from app.services import scheduler
from config import ADMIN_ID
logger = logging.getLogger(__name__)


SEP = "——————"


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def _check_admin(user_id: int, action: str) -> bool:
    """Defence-in-depth admin check that also logs every attempt."""
    return security.admin_only(ADMIN_ID, user_id, action)


def esc(value) -> str:
    """Shortcut for HTML-escaping user-controlled values."""
    return security.html_safe(value)


_INTER_MESSAGE_DELAY = 0.15


async def _ordered_gap():
    await asyncio.sleep(_INTER_MESSAGE_DELAY)


_replacement_timeouts: dict[int, asyncio.Task] = {}


def _cancel_replacement_timeout(user_id: int):
    task = _replacement_timeouts.pop(user_id, None)
    if task and not task.done():
        task.cancel()


async def _safe_search(message: Message, query: str, *, linked_only: bool):
    """
    Run a search and handle QueryTooLong with a friendly message.
    Returns list of dicts, or None if the query was rejected.
    """
    try:
        if linked_only:
            return await database.search_linked_engineers(query)
        return await database.search_engineers(query)
    except database.QueryTooLong as e:
        await message.answer(f"<i>{esc(e)}</i>")
        return None


async def _track_sent(
    assignment_id: int,
    engineer_id: int,
    chat_id: int,
    message_id: int,
    kind: str,
):
    """Record a message we just sent so we can nuke duplicates later."""
    try:
        await database.record_sent_message(assignment_id, engineer_id, chat_id, message_id, kind)
    except Exception:
        logger.exception("record_sent_message failed")


async def _nuke_other_messages(
    assignment_id: int,
    engineer_id: int,
    *,
    except_message_id: int,
    summary_text: str,
    kind: Optional[str] = None,
):
    """
    For every previously-sent message to this engineer for this assignment
    (excluding the one they're acting on), edit it to a final 'Ответ принят'
    state and remove its keyboard. Ignore errors (old / unmodifiable).
    """
    try:
        records = await database.get_sent_messages_for(assignment_id, engineer_id, kind=kind)
    except Exception:
        logger.exception("get_sent_messages_for failed")
        return
    for r in records:
        if r["message_id"] == except_message_id:
            continue
        try:
            await bot.edit_message_text(
                chat_id=r["chat_id"],
                message_id=r["message_id"],
                text=(
                    "<b>Ответ принят</b>\n"
                    "\n"
                    f"{summary_text}"
                ),
                reply_markup=None,
            )
        except Exception:
            # Older than 48h / not modifiable — ignore
            pass
    # Clear tracking so we don't try to edit again next time
    try:
        await database.delete_sent_messages_for(assignment_id, engineer_id, kind=kind)
    except Exception:
        pass


async def _reset_assignment_and_resend(assignment_id: int) -> dict:
    """
    Reset an assignment to a fresh 'pending' state and re-deliver the poll.
      - Edits the original engineer's old duty messages → 'Опрос обновлён'
      - Edits pending replacement candidates' messages → 'Запрос на замену отменён'
      - Clears sent_messages tracking, resets status & replacement chain
      - Sends a fresh poll to the original engineer
    Returns {old_status, delivered, engineer}.
    """
    a = await database.get_assignment(assignment_id)
    if not a:
        return {"old_status": None, "delivered": False, "engineer": None}

    old_status = a["status"]
    original = await database.get_engineer_by_id(a["engineer_id"])
    session = await database.get_duty_session(a["session_id"])
    period = session["period"] if session else "?"

    # Edit all previously-sent messages for this assignment
    records = await database.get_all_sent_messages_for_assignment(assignment_id)
    for r in records:
        if r["kind"] == "replacement":
            new_text = (
                "<b>Запрос на замену отменён</b>\n"
                "\n"
                "<i>Инициатор переотправил свой опрос. "
                "Ваше согласие больше не требуется.</i>"
            )
        else:
            new_text = (
                "<b>Опрос обновлён</b>\n"
                "\n"
                "<i>Администратор отправил вам новую версию опроса. "
                "Ответьте, пожалуйста, в новом сообщении.</i>"
            )
        try:
            await bot.edit_message_text(
                chat_id=r["chat_id"],
                message_id=r["message_id"],
                text=new_text,
                reply_markup=None,
            )
        except Exception:
            pass
    await database.delete_all_sent_messages_for_assignment(assignment_id)

    # Reset the assignment record itself
    await database.reset_assignment(assignment_id)
    # Per-project model: reset every project row for this engineer and void any
    # still-pending transfer requests they started, so a stale candidate answer
    # cannot land after the resend.
    await database.reset_engineer_projects(a["session_id"], a["engineer_id"])
    await database.cancel_pending_transfer_requests_for_initiator(
        a["session_id"], a["engineer_id"]
    )
    security.get_logger().info(
        f"ASSIGNMENT_RESET assignment={assignment_id} old_status={old_status!r}"
    )

    # Re-deliver the poll to the original engineer
    delivered = False
    if original and original.get("user_id"):
        projects = a["projects"]
        if projects:
            text = scheduler.format_duty_text(period, projects)
        else:
            text = (
                f"<b>Дежурство · <code>{esc(period)}</code></b>\n"
                "\n"
                "<i>Вы добавлены к опросу администратором вручную.</i>\n"
                "\n"
                "Проекты: <i>не указаны в графике</i>\n"
                "\n"
                f"{SEP}\n"
                "<i>Выберите действие ниже</i>"
            )
        try:
            sent_msg = await bot.send_message(
                original["user_id"], text,
                reply_markup=keyboards.duty_confirm_keyboard(assignment_id),
            )
            await _track_sent(
                assignment_id, original["id"], original["user_id"],
                sent_msg.message_id, "duty",
            )
            delivered = True
        except Exception:
            logger.exception("Fresh poll delivery failed after reset")

    return {"old_status": old_status, "delivered": delivered, "engineer": original}


async def _reject_already_answered(callback: CallbackQuery, summary_text: str):
    """User clicked a button on a stale message. Show neutral notice + nuke."""
    try:
        await callback.message.edit_text(
            "<b>Вы уже ответили</b>\n"
            "\n"
            f"Ваш текущий статус: <b>{summary_text}</b>\n"
            "\n"
            f"{SEP}\n"
            f"<i>Если нужно изменить ответ — обратитесь к {security.admin_mention()} "
            "или нажмите кнопку «Текущий опрос» в меню.</i>",
            reply_markup=None,
        )
    except Exception:
        pass
    await callback.answer("Вы уже ответили на этот опрос", show_alert=True)


async def _reject_if_cancelled(callback: CallbackQuery, session_id: int) -> bool:
    """
    If the session is cancelled, edit the message to show 'Опрос недействителен',
    remove the keyboard, and return True. Otherwise return False.
    """
    if not await database.is_session_cancelled(session_id):
        return False
    try:
        await callback.message.edit_text(
            "<b>Опрос недействителен</b>\n"
            "\n"
            "Этот опрос был отменён администратором.\n"
            "\n"
            f"{SEP}\n"
            "<i>Дождитесь следующего опроса. Когда он будет запущен, "
            "вам придёт уведомление.</i>",
            reply_markup=None,
        )
    except Exception:
        pass
    await callback.answer("Опрос отменён", show_alert=True)
    return True


async def _abort_if_cancelled(callback: CallbackQuery, state: FSMContext, session_id) -> bool:
    """
    Replacement-flow guard: if the session was cancelled mid-checklist, clear
    the FSM, blank the message and return True. Otherwise return False.
    """
    if session_id is None or not await database.is_session_cancelled(session_id):
        return False
    await state.clear()
    try:
        await callback.message.edit_text(
            "<b>Опрос недействителен</b>\n"
            "\n"
            "Этот опрос был отменён администратором.\n"
            "\n"
            f"{SEP}\n"
            "<i>Дождитесь следующего опроса.</i>",
            reply_markup=None,
        )
    except Exception:
        pass
    await callback.answer("Опрос отменён", show_alert=True)
    return True


def _surname_key(eng_or_name) -> str:
    """Sort key: by surname (first word of full_name), case-insensitive."""
    if isinstance(eng_or_name, dict):
        name = eng_or_name.get("full_name", "")
    else:
        name = str(eng_or_name)
    return name.lower()


def _render_engineer_lines(items: list, *, with_reason: bool, limit: int = 30) -> list[str]:
    """
    items: either list[dict engineer] or list[(dict engineer, reason text)]
    Returns lines like '· Иванов Иван (@ivanov)' or with ' — reason' suffix.
    """
    def sort_key(it):
        eng = it[0] if isinstance(it, tuple) else it
        return _surname_key(eng)
    items_sorted = sorted(items, key=sort_key)

    lines: list[str] = []
    shown = items_sorted[:limit]
    for it in shown:
        if isinstance(it, tuple):
            eng, reason = it
        else:
            eng, reason = it, None
        tag = eng.get("telegram_tag") or "нет тега"
        line = f"· {esc(eng['full_name'])} ({esc(tag)})"
        if with_reason and reason:
            line += f" — {esc(reason)}"
        lines.append(line)
    if len(items_sorted) > limit:
        lines.append(f"… и ещё {len(items_sorted) - limit} человек")
    return lines


async def _build_main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    """Compose main menu, asking DB whether session-dependent buttons should be visible."""
    # Active session controls session-dependent buttons for BOTH roles
    # (regular users get «Посмотреть график» only while a survey is active).
    session = await database.get_active_session()
    has_active = session is not None
    pending_requests = 0
    if is_admin:
        pending_requests = await database.count_pending_requests()
    return keyboards.main_menu_keyboard(
        is_admin, has_active_session=has_active, pending_requests=pending_requests,
    )


async def _remove_reply_keyboard(message: Message):
    """
    Remove any lingering persistent reply keyboard from earlier bot versions.
    A throwaway message is sent with ReplyKeyboardRemove, then deleted, so the
    keyboard disappears without leaving visible clutter.
    """
    try:
        tmp = await message.answer("⁣", reply_markup=ReplyKeyboardRemove())
        await tmp.delete()
    except Exception:
        pass


async def show_main_menu(message: Message, text: str):
    """Send the inline main menu as a fresh message, clearing any reply keyboard."""
    await _remove_reply_keyboard(message)
    kb = await _build_main_menu(_is_admin(message.from_user.id))
    await message.answer(text, reply_markup=kb)


def _name_tag(e: Optional[dict]) -> str:
    """'Имя Фамилия (@tag)' — escaped, ready for HTML."""
    if not e:
        return "?"
    tag = e.get("telegram_tag") or "нет тега"
    return esc(f"{e['full_name']} ({tag})")


async def _latest_transfer_candidate(session_id: int, ap_id: int) -> Optional[dict]:
    """The candidate of the most recent pending transfer request containing ap_id."""
    # Scan transfer_requests — simple approach for the modest data volume
    import json
    import aiosqlite
    async with aiosqlite.connect(database.DB_PATH) as db:
        async with db.execute(
            "SELECT candidate_engineer_id, project_ids FROM transfer_requests "
            "WHERE session_id=? AND status='pending' ORDER BY id DESC",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    for cand_id, pids_json in rows:
        try:
            pids = json.loads(pids_json or "[]")
        except Exception:
            pids = []
        if ap_id in pids:
            return await database.get_engineer_by_id(cand_id)
    return None


async def _project_names(ap_ids: list[int]) -> list[str]:
    names = []
    for ap_id in ap_ids:
        ap = await database.get_assignment_project(ap_id)
        if ap:
            names.append(ap["project_name"])
    return names


async def _check_session_complete(session_id: int):
    if await database.is_session_cancelled(session_id):
        return
    if not await scheduler.check_all_answered(session_id):
        return
    summary = await scheduler.build_summary(session_id)
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=summary,
        reply_markup=keyboards.finalize_keyboard(session_id),
    )
