"""Выгрузки графика: xlsx-снимок, PNG-картинка, публикация."""
import logging
import asyncio
from aiogram import F
from aiogram import Router
from aiogram.types import CallbackQuery, FSInputFile
from app import keyboards
from app.db import database
from app.handlers.helpers import (
    SEP, _check_admin, _is_admin, _ordered_gap, _render_engineer_lines, esc,
)
from app.loader import bot
from app.middlewares import security
from app.services import excel_parser
from app.services import scheduler
from config import ADMIN_ID

router = Router(name="schedule_output")
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "menu:export_now")
async def menu_export_now(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    session = await database.get_active_session()
    if not session:
        await callback.answer()
        await _ordered_gap()
        await bot.send_message(
            callback.message.chat.id,
            "<b>Нет активного опроса</b>\n"
            "\n"
            "<i>Сначала запустите дежурство.</i>",
        )
        return
    rows = await scheduler.build_current_state_data(session["id"])
    if not rows:
        await callback.answer()
        await _ordered_gap()
        await bot.send_message(callback.message.chat.id, "<i>В текущем опросе нет участников.</i>")
        return
    await callback.answer("Формирую файл...")
    await _ordered_gap()
    path = excel_parser.generate_schedule_xlsx(session["period"] + "_снимок", rows)
    doc = FSInputFile(path, filename=f"График_снимок_{session['period']}.xlsx")
    await bot.send_document(
        chat_id=callback.message.chat.id,
        document=doc,
        caption=(
            f"<b>Снимок опроса · <code>{esc(session['period'])}</code></b>\n"
            f"Записей: <b>{len(rows)}</b>"
        ),
    )


async def _project_coverage(session_id: int) -> tuple[int, int, int]:
    """Returns (covered, total, uncovered) project counts for a session."""
    aps = await database.get_session_assignment_projects(session_id)
    total = len(aps)
    covered = sum(
        1 for ap in aps
        if ap["status"] in (database.AP_CONFIRMED_SELF, database.AP_TRANSFER_ACCEPTED)
    )
    uncovered = sum(
        1 for ap in aps
        if ap["status"] in (database.AP_DECLINED, database.AP_NO_CONTACT)
    )
    return covered, total, uncovered


@router.callback_query(F.data == "menu:view_schedule")
async def menu_view_schedule(callback: CallbackQuery):
    """Available to any registered user (and admin). Sends the schedule image."""
    eng = await database.get_engineer_by_user_id(callback.from_user.id)
    if not eng and not _is_admin(callback.from_user.id):
        await callback.answer("Сначала привяжите аккаунт.", show_alert=True)
        return
    session = await database.get_active_session()
    if not session:
        await callback.answer("Сейчас нет опубликованного графика.", show_alert=True)
        return
    await callback.answer("Формирую график…")
    try:
        path = await scheduler.build_schedule_image(session["id"])
    except Exception:
        logger.exception("view_schedule render failed")
        await bot.send_message(callback.message.chat.id, "<i>Не удалось сформировать график.</i>")
        return
    if not path:
        await bot.send_message(callback.message.chat.id, "<i>В графике пока нет данных.</i>")
        return
    await _ordered_gap()
    photo = FSInputFile(path, filename=f"График_{session['period']}.png")
    await bot.send_photo(
        chat_id=callback.message.chat.id,
        photo=photo,
        caption=f"<b>График дежурств · <code>{esc(session['period'])}</code></b>",
    )


@router.callback_query(F.data == "menu:publish")
async def menu_publish(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action="menu:publish"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    session = await database.get_active_session()
    if not session:
        await callback.answer(
            "Нет активного графика для публикации. Сначала запустите опрос.",
            show_alert=True,
        )
        return
    covered, total, uncovered = await _project_coverage(session["id"])
    recipients = await database.count_linked_engineers()
    warn = ""
    if uncovered > 0:
        warn = (
            f"\n⚠️ <b>Внимание:</b> {uncovered} "
            f"{_plural_projects(uncovered)} пока без дежурного. Опубликовать всё равно?\n"
        )
    await callback.message.edit_text(
        "<b>Опубликовать график?</b>\n"
        "\n"
        f"Период: <b>{esc(session['period'])}</b>\n"
        f"График будет разослан всем зарегистрированным пользователям "
        f"({recipients} чел.).\n"
        f"{warn}"
        "\n"
        f"{SEP}\n"
        f"<i>Текущее состояние: закрыто {covered} из {total} проектов</i>",
        reply_markup=keyboards.confirm_publish_keyboard(session["id"]),
    )
    await callback.answer()


def _plural_projects(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "проект"
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return "проекта"
    return "проектов"


@router.callback_query(F.data.startswith("publish_confirm:"))
async def on_publish_confirm(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action="publish_confirm"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    session_id = int(callback.data.split(":")[1])
    session = await database.get_duty_session(session_id)
    if not session or session["finalized"] != database.SESSION_ACTIVE:
        await callback.answer("Опрос уже неактивен.", show_alert=True)
        return

    await callback.answer("Формирую график…")
    try:
        path = await scheduler.build_schedule_image(session_id)
    except Exception:
        logger.exception("publish render failed")
        await callback.message.edit_text("<i>Не удалось сформировать график.</i>")
        return
    if not path:
        await callback.message.edit_text("<i>В графике нет данных для публикации.</i>")
        return

    period = session["period"]
    recipients = await database.get_linked_engineers()
    caption = (
        f"<b>График дежурств · <code>{esc(period)}</code></b>\n"
        "<i>Опубликован администратором</i>"
    )

    async def _send(eng: dict):
        try:
            await bot.send_photo(
                chat_id=eng["user_id"],
                photo=FSInputFile(path, filename=f"График_{period}.png"),
                caption=caption,
            )
            return (eng, True)
        except Exception:
            logger.exception(f"publish delivery failed to engineer {eng['id']}")
            return (eng, False)

    results = await asyncio.gather(*(_send(e) for e in recipients))
    delivered = [e for e, ok in results if ok]
    failed = [e for e, ok in results if not ok]

    security.get_logger().info(
        f"SCHEDULE_PUBLISHED session={session_id} sent={len(delivered)} failed={len(failed)}"
    )

    parts = [
        "<b>График опубликован</b>",
        "",
        f"<pre>Отправлено:    {len(delivered):>3}\n"
        f"Не доставлено: {len(failed):>3}</pre>",
    ]
    if delivered:
        parts.append("")
        parts.append("<b>Отправлено</b>")
        parts.append("<pre>" + "\n".join(_render_engineer_lines(delivered, with_reason=False)) + "</pre>")
    if failed:
        parts.append("")
        parts.append("<b>Не доставлено</b>")
        parts.append("<pre>" + "\n".join(_render_engineer_lines(failed, with_reason=False)) + "</pre>")
    parts.append("")
    parts.append(SEP)
    parts.append("<i>Готово</i>")

    try:
        await callback.message.edit_text("\n".join(parts), reply_markup=keyboards.back_keyboard())
    except Exception:
        pass


@router.callback_query(F.data.startswith("create_schedule:"))
async def on_create_schedule(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    session_id = int(callback.data.split(":")[1])
    session = await database.get_duty_session(session_id)
    period = session["period"] if session else "unknown"

    rows = await scheduler.build_schedule_data(session_id)
    if not rows:
        await callback.message.edit_text(
            (callback.message.html_text or "")
            + f"\n\n{SEP}\n<i>Нет подтверждённых дежурных для создания графика.</i>"
        )
        await callback.answer()
        return

    path = excel_parser.generate_schedule_xlsx(period, rows)
    await database.finalize_session(session_id)

    await callback.message.edit_text(
        (callback.message.html_text or "")
        + f"\n\n{SEP}\n<b>График создан</b>"
    )
    await callback.answer()
    await _ordered_gap()

    doc = FSInputFile(path, filename=f"График_{period}.xlsx")
    await bot.send_document(
        chat_id=ADMIN_ID,
        document=doc,
        caption=f"<b>График дежурств · <code>{esc(period)}</code></b>",
    )


@router.callback_query(F.data.startswith("skip_schedule:"))
async def on_skip_schedule(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    session_id = int(callback.data.split(":")[1])
    await database.finalize_session(session_id)
    await callback.message.edit_text(
        (callback.message.html_text or "")
        + f"\n\n{SEP}\n<i>График не создаётся.</i>"
    )
    await callback.answer()
