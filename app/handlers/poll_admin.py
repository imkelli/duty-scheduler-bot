"""Жизненный цикл опроса: запуск, отмена, напоминания, персональная отправка."""
import logging
import os
from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from typing import Optional
from app import keyboards
from app.db import database
from app.handlers.helpers import (
    SEP, _build_main_menu, _cancel_replacement_timeout, _check_admin, _is_admin, _name_tag, _ordered_gap, _project_names, _render_engineer_lines, _reset_assignment_and_resend, _track_sent, esc,
)
from app.loader import bot
from app.loader import dp
from app.middlewares import security
from app.services import excel_parser
from app.services import scheduler
from app.states import DutyStates
from config import EXCEL_FILE

router = Router(name="poll_admin")
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "menu:duty")
async def menu_duty(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await _ordered_gap()
    await _do_duty(callback.message.chat.id)


@router.callback_query(F.data == "menu:cancel_poll")
async def menu_cancel_poll(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    session = await database.get_active_session()
    if not session:
        await callback.answer("Нет активного опроса.", show_alert=True)
        return
    # Считаем по assignment_projects (источник истины), по уникальным
    # инженерам: ответившим считается тот, у кого ВСЕ проекты не pending.
    answered, total = await database.count_session_progress(session["id"])
    await callback.message.edit_text(
        "<b>Отменить текущий опрос?</b>\n"
        "\n"
        f"Период: <b>{esc(session['period'])}</b>\n"
        f"Получено ответов: <b>{answered}</b> из <b>{total}</b>\n"
        "\n"
        f"{SEP}\n"
        "<i>Все собранные ответы будут утрачены. Опрос станет недействительным для всех участников.</i>",
        reply_markup=keyboards.confirm_cancel_poll_keyboard(session["id"]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_poll_confirm:"))
async def on_cancel_poll_confirm(callback: CallbackQuery, state: FSMContext):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    session_id = int(callback.data.split(":")[1])
    session = await database.get_duty_session(session_id)
    if not session or session["finalized"] != database.SESSION_ACTIVE:
        await callback.answer("Опрос уже неактивен.", show_alert=True)
        return
    await database.cancel_session(session_id)
    security.get_logger().warning(
        f"POLL_CANCELLED session={session_id} by={security.mask_user_id(callback.from_user.id)}"
    )
    # Clear our own FSM if it referenced this session (replacement search etc.)
    cur = await state.get_state()
    if cur:
        await state.clear()
    _cancel_replacement_timeout(callback.from_user.id)
    await callback.message.edit_text(
        f"<b>Опрос отменён</b>\n"
        "\n"
        f"Период: <code>{esc(session['period'])}</code>",
        reply_markup=keyboards.back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_poll_cancel")
async def on_cancel_poll_cancel(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>Главное меню</b>",
        reply_markup=await _build_main_menu(_is_admin(callback.from_user.id)),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:recreate")
async def menu_recreate(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    session = await database.get_active_session()
    if not session:
        await callback.answer("Нет активного опроса для сброса.", show_alert=True)
        return
    await callback.message.edit_text(
        f"<b>Сброс опроса</b>\n"
        "\n"
        f"Текущий опрос за период <code>{esc(session['period'])}</code> будет удалён вместе со всеми ответами.\n"
        "\n"
        f"{SEP}\n"
        "<i>Продолжить?</i>",
        reply_markup=keyboards.confirm_recreate_keyboard(session["id"]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("recreate_confirm:"))
async def on_recreate_confirm(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    session_id = int(callback.data.split(":")[1])
    await database.delete_session(session_id)
    await callback.message.edit_text("<i>Опрос удалён. Выберите новый период.</i>")
    await callback.answer()
    await _ordered_gap()
    await _do_duty(callback.message.chat.id)


@router.callback_query(F.data == "recreate_cancel")
async def on_recreate_cancel(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>Главное меню</b>",
        reply_markup=await _build_main_menu(_is_admin(callback.from_user.id)),
    )
    await callback.answer()


REMIND_COOLDOWN_SEC = 30 * 60


_last_remind_ts: dict[int, float] = {}  # session_id → epoch seconds


@router.callback_query(F.data == "menu:remind")
async def menu_remind(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action="menu:remind"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    session = await database.get_active_session()
    if not session:
        await callback.answer("Нет активного опроса.", show_alert=True)
        return

    import time as _time
    last = _last_remind_ts.get(session["id"], 0.0)
    elapsed = _time.time() - last
    if elapsed < REMIND_COOLDOWN_SEC:
        wait_min = int((REMIND_COOLDOWN_SEC - elapsed) // 60) + 1
        await callback.answer(
            f"Следующее напоминание можно отправить через {wait_min} мин.",
            show_alert=True,
        )
        return

    period = session["period"]
    aps = await database.get_session_assignment_projects(session["id"])
    legacy = await database.get_session_assignments(session["id"])
    assignment_by_eng = {a["engineer_id"]: a["id"] for a in legacy}

    # Originals who still have pending projects (per-project model)
    pending_by_eng: dict[int, list[str]] = {}
    for ap in aps:
        if ap["status"] == database.AP_PENDING:
            pending_by_eng.setdefault(ap["engineer_id"], []).append(ap["project_name"])

    # Candidates with an unanswered transfer request
    pending_reqs = await database.get_pending_transfer_requests(session["id"])

    if not pending_by_eng and not pending_reqs:
        await callback.answer("Все уже ответили.", show_alert=True)
        return

    reached: list[dict] = []
    unreached: list[tuple[dict, str]] = []

    # 1. Remind original duty officers with still-pending projects
    for eng_id, projects in pending_by_eng.items():
        eng = await database.get_engineer_by_id(eng_id)
        if not eng:
            continue
        if not eng.get("user_id"):
            unreached.append((eng, "не зарегистрирован"))
            continue
        a_id = assignment_by_eng.get(eng_id)
        if a_id is None:
            unreached.append((eng, "нет записи опроса"))
            continue
        projects_block = "\n".join(f"· {esc(p)}" for p in projects)
        try:
            sent_msg = await bot.send_message(
                eng["user_id"],
                f"<b>Напоминание · <code>{esc(period)}</code></b>\n"
                "\n"
                "Вы ещё не ответили на запрос о дежурстве.\n"
                "\n"
                "Проекты:\n"
                f"{projects_block}\n"
                "\n"
                f"{SEP}\n"
                "<i>Пожалуйста, выберите действие ниже</i>",
                reply_markup=keyboards.duty_confirm_keyboard(a_id),
            )
            await _track_sent(a_id, eng_id, eng["user_id"], sent_msg.message_id, "duty")
            reached.append(eng)
        except Exception:
            logger.exception(f"Reminder delivery failed to engineer {eng_id}")
            unreached.append((eng, "ошибка отправки"))

    # 2. Remind transfer candidates who haven't answered a transfer request
    for req in pending_reqs:
        cand = await database.get_engineer_by_id(req["candidate_engineer_id"])
        if not cand:
            continue
        if not cand.get("user_id"):
            unreached.append((cand, "не зарегистрирован"))
            continue
        initiator = await database.get_engineer_by_id(req["initiator_engineer_id"])
        names = await _project_names(req["project_ids"])
        projects_block = "\n".join(f"· {esc(n)}" for n in names) or "· —"
        try:
            await bot.send_message(
                cand["user_id"],
                f"<b>Напоминание · замена · <code>{esc(period)}</code></b>\n"
                "\n"
                f"<b>За кого:</b> {_name_tag(initiator)}\n"
                "\n"
                "Вы ещё не ответили на предложение подменить дежурство.\n"
                "\n"
                f"Проекты:\n{projects_block}\n"
                "\n"
                f"{SEP}\n"
                "<i>Пожалуйста, выберите действие ниже</i>",
                reply_markup=keyboards.transfer_response_keyboard(req["id"]),
            )
            reached.append(cand)
        except Exception:
            logger.exception(f"Transfer reminder delivery failed req={req['id']}")
            unreached.append((cand, "ошибка отправки"))

    _last_remind_ts[session["id"]] = _time.time()
    security.get_logger().info(
        f"REMIND session={session['id']} reached={len(reached)} unreached={len(unreached)}"
    )

    parts = [
        f"<b>Напоминания · <code>{esc(period)}</code></b>",
        "",
        f"<pre>Получили:      {len(reached):>3}\n"
        f"Не доставлено: {len(unreached):>3}</pre>",
    ]
    if reached:
        parts.append("")
        parts.append("<b>Получили</b>")
        parts.append("<pre>" + "\n".join(_render_engineer_lines(reached, with_reason=False)) + "</pre>")
    if unreached:
        parts.append("")
        parts.append("<b>Не доставлено</b>")
        parts.append("<pre>" + "\n".join(_render_engineer_lines(unreached, with_reason=True)) + "</pre>")
    parts.append("")
    parts.append(SEP)
    parts.append("<i>Повторно — через 30 минут</i>")

    try:
        await callback.message.edit_text("\n".join(parts), reply_markup=keyboards.back_keyboard())
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "menu:resend")
async def menu_resend(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action="menu:resend"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    session = await database.get_active_session()
    if not session:
        await callback.answer("Нет активного опроса.", show_alert=True)
        return

    period = session["period"]
    session_id = session["id"]

    # 1. Read fresh duty list from Excel
    periods = excel_parser.get_periods(EXCEL_FILE, filter_weeks=3)
    col_index = next((c for c, label in periods if label == period), None)
    if col_index is None:
        # Period may be outside the 4-week window now; scan all periods
        all_periods = excel_parser.get_periods(EXCEL_FILE)
        col_index = next((c for c, label in all_periods if label == period), None)
    if col_index is None:
        await callback.answer("Не удалось найти период в Excel.", show_alert=True)
        return
    try:
        duty_map = excel_parser.get_duty_map(EXCEL_FILE, col_index)
    except excel_parser.ExcelError as e:
        await callback.message.answer(f"<b>Ошибка чтения Excel</b>\n{e}")
        await callback.answer()
        return

    # Apply plan-ahead replacements
    all_engineers = await database.get_all_engineers()
    by_id = {e["id"]: e for e in all_engineers}
    by_name = {e["full_name"]: e for e in all_engineers}
    accepted_reps = await database.get_active_pending_replacements_for_period(period)
    for rep in accepted_reps:
        orig = by_id.get(rep["original_engineer_id"])
        cand = by_id.get(rep["replacement_engineer_id"])
        if not orig or not cand:
            continue
        projects = duty_map.pop(orig["full_name"], None)
        if projects is None:
            continue
        duty_map.setdefault(cand["full_name"], []).extend(projects)

    # 2. Existing assignments — map engineer_id → assignment
    existing = await database.get_session_assignments(session_id)
    a_by_eng = {a["engineer_id"]: a for a in existing}

    # 3. Find category (a) new, (b) re-registered
    to_send: list[tuple[dict, list[str], str]] = []  # (eng, projects, reason)
    not_registered_now: list[dict] = []
    for name, projects in duty_map.items():
        eng = by_name.get(name)
        if not eng:
            continue
        a = a_by_eng.get(eng["id"])
        if not a:
            # category (a): never had an assignment in this session
            if eng.get("user_id"):
                to_send.append((eng, projects, "новый участник"))
            else:
                not_registered_now.append(eng)
        elif a["status"] in ("no_user_id", "no_telegram") and eng.get("user_id"):
            # category (b): had a stub assignment marked as unregistered;
            # they have linked since
            to_send.append((eng, projects, "зарегистрировался(-лась) после старта опроса"))
        # else: already received the poll — skip

    if not to_send and not not_registered_now:
        await callback.message.edit_text(
            f"<b>Дополнительная рассылка · <code>{esc(period)}</code></b>\n"
            "\n"
            "<i>Все актуальные участники уже получили опрос. "
            "Дублирующая рассылка не выполнена.</i>",
            reply_markup=keyboards.back_keyboard(),
        )
        await callback.answer()
        return

    # 4. Send
    sent_now: list[tuple[dict, str]] = []  # (eng, reason)
    failed: list[dict] = []
    for eng, projects, reason in to_send:
        # Either create new assignment, or flip the existing one to pending and update final_engineer
        a = a_by_eng.get(eng["id"])
        if a is None:
            assignment_id = await database.create_assignment(session_id, eng["id"], projects)
            await database.create_assignment_projects(session_id, eng["id"], projects)
        else:
            assignment_id = a["id"]
            # Full reset so a previously-stuck status (no_user_id / chain) won't
            # block the engineer from answering the fresh poll.
            await database.reset_assignment(assignment_id)
            # Per-project model: reset existing rows, or create them if missing.
            existing_aps = await database.get_projects_for_engineer(session_id, eng["id"])
            if existing_aps:
                await database.reset_engineer_projects(session_id, eng["id"])
            else:
                await database.create_assignment_projects(session_id, eng["id"], projects)
        text = scheduler.format_duty_text(period, projects)
        try:
            sent_msg = await bot.send_message(
                eng["user_id"], text,
                reply_markup=keyboards.duty_confirm_keyboard(assignment_id),
            )
            await _track_sent(assignment_id, eng["id"], eng["user_id"], sent_msg.message_id, "duty")
            sent_now.append((eng, reason))
        except Exception:
            logger.exception(f"Resend delivery failed to engineer {eng['id']}")
            failed.append(eng)
            await database.update_assignment_status(assignment_id, "unreachable")

    security.get_logger().info(
        f"RESEND session={session_id} sent={len(sent_now)} not_registered={len(not_registered_now)} failed={len(failed)}"
    )

    lines = [
        f"<b>Дополнительная рассылка · <code>{esc(period)}</code></b>",
        "",
        f"<pre>Найдено новых:  {len(to_send) + len(not_registered_now):>3}\n"
        f"Отправлено:     {len(sent_now):>3}\n"
        f"Не зарегистр.:  {len(not_registered_now):>3}</pre>",
    ]
    if sent_now:
        # Render with reason as suffix
        items = [(eng, reason) for eng, reason in sent_now]
        lines.append("")
        lines.append("<b>Отправлено</b>")
        lines.append("<pre>" + "\n".join(_render_engineer_lines(items, with_reason=True)) + "</pre>")
    if not_registered_now:
        lines.append("")
        lines.append("<b>Не зарегистрированы</b>")
        lines.append("<pre>" + "\n".join(_render_engineer_lines(not_registered_now, with_reason=False)) + "</pre>")
    if failed:
        lines.append("")
        lines.append("<b>Не доставлено</b>")
        lines.append("<pre>" + "\n".join(_render_engineer_lines(failed, with_reason=False)) + "</pre>")

    try:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=keyboards.back_keyboard(),
        )
    except Exception:
        pass
    await callback.answer()


def _projects_for_engineer(period: str, full_name: str) -> list[str]:
    """Look up the engineer's projects for the given period in the Excel schedule."""
    periods = excel_parser.get_periods(EXCEL_FILE)  # all periods (no week filter)
    col = next((c for c, label in periods if label == period), None)
    if col is None:
        return []
    duty_map = excel_parser.get_duty_map(EXCEL_FILE, col)
    return duty_map.get(full_name, [])


_DUTY_STATUS_LABEL = {
    "pending":      "ожидает ответа",
    "confirmed":    "подтверждено",
    "declined":     "отказ",
    "chain_failed": "цепочка замен исчерпана",
    "no_telegram":  "нет Telegram",
    "no_user_id":   "не запустил бота",
    "unreachable":  "ошибка отправки",
}


@router.callback_query(F.data == "menu:send_personal")
async def menu_send_personal(callback: CallbackQuery, state: FSMContext):
    if not _check_admin(callback.from_user.id, action="menu:send_personal"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    session = await database.get_active_session()
    if not session:
        await callback.answer("Нет активного опроса.", show_alert=True)
        return
    await state.set_state(DutyStates.waiting_personal_query)
    await callback.message.edit_text(
        "<b>Кому отправить опрос?</b>\n"
        "\n"
        "Введите имя, фамилию или @тег.\n"
        "\n"
        f"{SEP}\n"
        "<i>Поиск работает по всей базе инженеров</i>",
        reply_markup=keyboards.back_keyboard(),
    )
    await callback.answer()


@router.message(DutyStates.waiting_personal_query)
async def on_personal_query(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id, action="personal_query"):
        await state.clear()
        return
    query = security.sanitize_text(message.text)
    if not query:
        await message.answer("<i>Пустой ввод. Попробуйте ещё раз.</i>")
        return
    try:
        results = await database.search_engineers(query)
    except database.QueryTooLong as e:
        await message.answer(f"<i>{esc(e)}</i>", reply_markup=keyboards.back_keyboard())
        return
    if not results:
        await message.answer(
            "<i>Никого не найдено. Попробуйте другой запрос или нажмите «Назад».</i>",
            reply_markup=keyboards.back_keyboard(),
        )
        return
    # State stays active so the admin can refine the search by typing again
    shown = results[:15]
    suffix = "+" if len(results) > 15 else ""
    await message.answer(
        f"<b>Найдено: {len(shown)}{suffix}</b>",
        reply_markup=keyboards.personal_candidates_keyboard(shown),
    )


@router.callback_query(F.data.startswith("personal_pick:"))
async def on_personal_pick(callback: CallbackQuery, state: FSMContext):
    if not _check_admin(callback.from_user.id, action="personal_pick"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    eng_id = int(callback.data.split(":")[1])
    eng = await database.get_engineer_by_id(eng_id)
    if not eng:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    session = await database.get_active_session()
    if not session:
        await callback.answer("Опрос уже неактивен.", show_alert=True)
        return

    # (a) not registered in the bot
    if not eng.get("user_id"):
        await callback.message.edit_text(
            "<b>Невозможно отправить</b>\n"
            "\n"
            f"{esc(eng['full_name'])} не зарегистрирован в боте (не делал /start).\n"
            "Попросите его запустить бота и попробуйте снова.",
            reply_markup=keyboards.back_keyboard(),
        )
        await callback.answer()
        return

    # (c) already has an assignment in this session
    assignments = await database.get_session_assignments(session["id"])
    existing = next((x for x in assignments if x["engineer_id"] == eng_id), None)
    if existing:
        # If a replacement was passed, the readable status is "Передано замене"
        if existing["status"] == "pending" and existing["replacement_chain"]:
            label = "Передано замене"
        else:
            label = _DUTY_STATUS_LABEL.get(existing["status"], existing["status"])
        await callback.message.edit_text(
            "<b>Повторная отправка</b>\n"
            "\n"
            f"<b>{esc(eng['full_name'])}</b> уже получил опрос.\n"
            f"Текущий статус: <b>{esc(label)}</b>\n"
            "\n"
            f"{SEP}\n"
            "<i>Что сделать?</i>",
            reply_markup=keyboards.personal_confirm_keyboard(eng_id, "resend"),
        )
        await callback.answer()
        return

    # Determine projects from the schedule
    try:
        projects = _projects_for_engineer(session["period"], eng["full_name"])
    except excel_parser.ExcelError as e:
        await callback.message.edit_text(
            f"<b>Ошибка чтения Excel</b>\n{e}",
            reply_markup=keyboards.back_keyboard(),
        )
        await callback.answer()
        return

    # (b) not on duty this period
    if not projects:
        await callback.message.edit_text(
            "<b>Подтверждение</b>\n"
            "\n"
            f"{esc(eng['full_name'])} не назначен дежурным на период "
            f"<code>{esc(session['period'])}</code> по графику.\n"
            "Всё равно отправить опрос?",
            reply_markup=keyboards.personal_confirm_keyboard(eng_id, "force"),
        )
        await callback.answer()
        return

    # (d) normal — send straight away
    await _do_personal_send(callback, session, eng, projects, manual=False, existing=None)


@router.callback_query(F.data.startswith("personal_send:"))
async def on_personal_send(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action="personal_send"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    parts = callback.data.split(":")
    eng_id = int(parts[1])
    mode = parts[2]  # 'force' | 'resend'
    eng = await database.get_engineer_by_id(eng_id)
    session = await database.get_active_session()
    if not eng or not session:
        await callback.answer("Контекст утерян.", show_alert=True)
        return
    if not eng.get("user_id"):
        await callback.answer("Пользователь не зарегистрирован.", show_alert=True)
        return

    if mode == "resend":
        assignments = await database.get_session_assignments(session["id"])
        existing = next((x for x in assignments if x["engineer_id"] == eng_id), None)
        if not existing:
            # Assignment vanished — fall back to a fresh send
            try:
                projects = _projects_for_engineer(session["period"], eng["full_name"])
            except excel_parser.ExcelError:
                projects = []
            await _do_personal_send(callback, session, eng, projects,
                                    manual=not projects, existing=None)
            return
        # Full reset: clear status + chain, blank old messages, re-deliver
        result = await _reset_assignment_and_resend(existing["id"])
        old_label = _DUTY_STATUS_LABEL.get(result["old_status"], result["old_status"] or "—")
        tag = eng.get("telegram_tag") or "тег отсутствует"
        if result["delivered"]:
            await callback.message.edit_text(
                "<b>Опрос переотправлен</b>\n"
                "\n"
                f"Получатель: <b>{esc(eng['full_name'])}</b> ({esc(tag)})\n"
                f"Старый статус: <b>{esc(old_label)}</b> → сброшен",
                reply_markup=keyboards.back_keyboard(),
            )
        else:
            await callback.message.edit_text(
                "<b>Не удалось переотправить</b>\n"
                "\n"
                f"Статус {esc(eng['full_name'])} сброшен, но сообщение не доставлено. "
                "Возможно, пользователь заблокировал бота.",
                reply_markup=keyboards.back_keyboard(),
            )
        await callback.answer()
    else:  # force
        await _do_personal_send(callback, session, eng, [], manual=True, existing=None)


async def _do_personal_send(
    callback: CallbackQuery,
    session: dict,
    eng: dict,
    projects: list[str],
    *,
    manual: bool,
    existing: Optional[dict],
):
    """Create/reuse an assignment and deliver the poll, then confirm to the admin."""
    period = session["period"]
    if existing is not None:
        assignment_id = existing["id"]
    else:
        assignment_id = await database.create_assignment(session["id"], eng["id"], projects)
        # Force-send without schedule projects: still create one placeholder
        # project row so the person can actually confirm/decline a duty.
        ap_projects = projects if projects else ["Дежурство (вне графика)"]
        await database.create_assignment_projects(session["id"], eng["id"], ap_projects)

    if manual:
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
    else:
        text = scheduler.format_duty_text(period, projects)

    delivered = True
    try:
        sent_msg = await bot.send_message(
            eng["user_id"], text,
            reply_markup=keyboards.duty_confirm_keyboard(assignment_id),
        )
        await _track_sent(assignment_id, eng["id"], eng["user_id"], sent_msg.message_id, "duty")
    except Exception:
        logger.exception("personal poll delivery failed")
        delivered = False
        if existing is None:
            await database.update_assignment_status(assignment_id, "unreachable")

    security.get_logger().info(
        f"PERSONAL_SEND session={session['id']} engineer_id={eng['id']} "
        f"manual={manual} resend={existing is not None} delivered={delivered}"
    )

    tag = eng.get("telegram_tag") or "тег отсутствует"
    projects_str = ", ".join(projects) if projects else "не указаны"
    if delivered:
        await callback.message.edit_text(
            "<b>Опрос отправлен</b>\n"
            "\n"
            f"Получатель: <b>{esc(eng['full_name'])}</b> ({esc(tag)})\n"
            f"Проекты: {esc(projects_str)}\n"
            "\n"
            f"{SEP}\n"
            "<i>Запись добавлена в активный опрос</i>",
            reply_markup=keyboards.back_keyboard(),
        )
    else:
        await callback.message.edit_text(
            "<b>Не удалось отправить</b>\n"
            "\n"
            f"Сообщение для {esc(eng['full_name'])} не доставлено. "
            "Возможно, пользователь заблокировал бота.",
            reply_markup=keyboards.back_keyboard(),
        )
    await callback.answer()


async def _do_duty(chat_id: int):
    if not os.path.exists(EXCEL_FILE):
        await bot.send_message(chat_id, f"<b>Ошибка</b>\nФайл <code>{EXCEL_FILE}</code> не найден.")
        return
    try:
        periods = excel_parser.get_periods(EXCEL_FILE, filter_weeks=3)
    except excel_parser.ExcelError as e:
        await bot.send_message(chat_id, f"<b>Ошибка чтения Excel</b>\n{e}")
        return
    except Exception:
        logger.exception("Period parsing failed")
        await bot.send_message(chat_id, "<b>Ошибка чтения Excel</b>\n<i>Подробности см. в логах.</i>")
        return
    if not periods:
        await bot.send_message(chat_id, "<i>Периоды дежурств не найдены.</i>")
        return
    await bot.send_message(
        chat_id,
        "<b>Выберите период дежурства</b>",
        reply_markup=keyboards.periods_keyboard(periods),
    )


@router.message(Command("duty"))
async def cmd_duty(message: Message):
    if not _check_admin(message.from_user.id, action="cmd"):
        return
    await _do_duty(message.chat.id)


@router.callback_query(F.data.startswith("period:"))
async def on_period_selected(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    _, col_str, *label_parts = callback.data.split(":")
    col_index = int(col_str)
    period = ":".join(label_parts)

    await callback.message.edit_text(f"<i>Загружаю данные для периода <code>{esc(period)}</code>…</i>")

    try:
        duty_map = excel_parser.get_duty_map(EXCEL_FILE, col_index)
    except excel_parser.ExcelError as e:
        await callback.message.answer(f"<b>Ошибка чтения Excel</b>\n{e}")
        await callback.answer()
        return
    except Exception:
        logger.exception("Duty map parsing failed")
        await callback.message.answer("<b>Ошибка чтения данных</b>\n<i>Подробности см. в логах.</i>")
        await callback.answer()
        return

    if not duty_map:
        await callback.message.answer("<i>На выбранный период дежурных нет.</i>")
        await callback.answer()
        return

    session_id = await database.create_duty_session(period)

    # Single DB query — build a name → engineer lookup table
    all_engineers = await database.get_all_engineers()
    by_name = {e["full_name"]: e for e in all_engineers}
    by_id = {e["id"]: e for e in all_engineers}

    # Apply pending plan-ahead replacements: substitute original name in duty_map
    # with the accepted candidate's name.
    plan_subs: list[str] = []  # human-readable list for the admin summary
    pending_reps = await database.get_active_pending_replacements_for_period(period)
    for rep in pending_reps:
        orig_eng = by_id.get(rep["original_engineer_id"])
        cand_eng = by_id.get(rep["replacement_engineer_id"])
        if not orig_eng or not cand_eng:
            continue
        orig_name = orig_eng["full_name"]
        cand_name = cand_eng["full_name"]
        projects = duty_map.pop(orig_name, None)
        if projects is None:
            continue
        # Merge into candidate's existing duties (if any)
        duty_map.setdefault(cand_name, []).extend(projects)
        plan_subs.append(f"{orig_name} → {cand_name}")
        await database.mark_pending_replacement_applied(rep["id"])

    engineers_info: list[dict] = []
    no_db: list[str] = []
    for name in duty_map:
        eng = by_name.get(name)
        if eng:
            engineers_info.append(eng)
        else:
            no_db.append(name)

    result = await scheduler.send_duty_notifications(
        bot, session_id, period, duty_map, engineers_info
    )
    sent_list = result["sent"]
    skipped_list = result["skipped"]

    lines = [
        f"<b>Рассылка · <code>{esc(period)}</code></b>",
        "",
        f"<pre>Всего:        {len(duty_map):>3}\n"
        f"Отправлено:   {len(sent_list):>3}\n"
        f"Пропущено:    {len(skipped_list):>3}\n"
        f"Нет в базе:   {len(no_db):>3}</pre>",
    ]
    if plan_subs:
        lines.append("")
        lines.append("<b>Плановые замены</b>")
        lines.append("<pre>" + "\n".join(f"· {esc(s)}" for s in plan_subs) + "</pre>")
    if no_db:
        lines.append("")
        lines.append("<b>Не найдены в базе</b>")
        lines.append("<pre>" + "\n".join(f"· {esc(n)}" for n in sorted(no_db, key=str.lower)) + "</pre>")
    if sent_list:
        lines.append("")
        lines.append("<b>Отправлено</b>")
        lines.append("<pre>" + "\n".join(_render_engineer_lines(sent_list, with_reason=False)) + "</pre>")
    if skipped_list:
        lines.append("")
        lines.append("<b>Пропущены</b>")
        lines.append("<pre>" + "\n".join(_render_engineer_lines(skipped_list, with_reason=True)) + "</pre>")
    lines.append("")
    lines.append(SEP)
    lines.append("<i>Ожидание ответов от инженеров.</i>")

    await callback.message.answer("\n".join(lines))
    await callback.answer()

    dp["active_session"] = session_id
    dp["active_period"] = period
