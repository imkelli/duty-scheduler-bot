"""Плановые замены «заранее» (отпуск/командировка)."""
import logging
from aiogram import F
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from app import keyboards
from app.db import database
from app.handlers.helpers import (
    SEP, esc,
)
from app.loader import bot
from app.middlewares import security
from app.services import excel_parser
from app.services.notify import (
    _notify_admin,
)
from app.states import DutyStates
from config import ADMIN_ID, EXCEL_FILE

router = Router(name="planned")
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "menu:req_replace")
async def menu_req_replace(callback: CallbackQuery, state: FSMContext):
    eng = await database.get_engineer_by_user_id(callback.from_user.id)
    if not eng:
        await callback.answer("Сначала привяжите аккаунт.", show_alert=True)
        return
    # Find weeks where user is on duty for next 4 weeks
    try:
        rows = excel_parser.get_user_duties(EXCEL_FILE, eng["full_name"], filter_weeks=3)
    except excel_parser.ExcelError as e:
        await callback.message.edit_text(
            f"<b>Ошибка чтения Excel</b>\n{e}",
            reply_markup=keyboards.back_keyboard(),
        )
        await callback.answer()
        return

    duty_weeks: list[tuple[int, str]] = []
    if rows:
        # We need col_index too — re-derive via get_periods
        periods = excel_parser.get_periods(EXCEL_FILE, filter_weeks=3)
        by_label = {label: col for col, label in periods}
        for label, projects in rows:
            if projects and label in by_label:
                duty_weeks.append((by_label[label], label))

    if not duty_weeks:
        await callback.message.edit_text(
            "<b>Запрос на замену</b>\n"
            "\n"
            "<i>На ближайшие 4 недели вы не назначены дежурным.</i>",
            reply_markup=keyboards.back_keyboard(),
        )
        await callback.answer()
        return

    await state.update_data(req_eng_id=eng["id"])
    await callback.message.edit_text(
        "<b>Запрос на замену</b>\n"
        "\n"
        "На каких неделях вам нужна замена?\n"
        "\n"
        f"{SEP}\n"
        "<i>Выберите неделю</i>",
        reply_markup=keyboards.req_replace_weeks_keyboard(duty_weeks),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("req_replace_week:"))
async def on_req_replace_week(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":", 2)
    col_index = int(parts[1])
    label = parts[2]
    eng = await database.get_engineer_by_user_id(callback.from_user.id)
    if not eng:
        await callback.answer("Сначала привяжите аккаунт.", show_alert=True)
        return
    try:
        duty_map = excel_parser.get_duty_map(EXCEL_FILE, col_index)
    except excel_parser.ExcelError as e:
        await callback.message.edit_text(
            f"<b>Ошибка чтения Excel</b>\n{e}",
            reply_markup=keyboards.back_keyboard(),
        )
        await callback.answer()
        return
    projects = duty_map.get(eng["full_name"], [])
    if not projects:
        await callback.answer("На этой неделе вы не дежурите.", show_alert=True)
        return

    await state.set_state(DutyStates.waiting_request_reason)
    await state.update_data(req_period=label, req_projects=projects)

    projects_block = "\n".join(f"· {esc(p)}" for p in projects)
    await callback.message.edit_text(
        f"<b>{esc(label)}</b>\n"
        "\n"
        f"Проекты:\n{projects_block}\n"
        "\n"
        f"{SEP}\n"
        "<i>Опишите причину (отпуск, командировка, болезнь и т.д.)</i>",
        reply_markup=keyboards.back_keyboard(),
    )
    await callback.answer()


@router.message(DutyStates.waiting_request_reason)
async def on_request_reason_text(message: Message, state: FSMContext):
    reason = security.sanitize_text(message.text, max_length=500)
    if not reason:
        await message.answer("<i>Пустой ввод. Введите причину или нажмите «Назад».</i>")
        return
    await state.update_data(req_reason=reason)
    await state.set_state(None)  # leave FSM but keep data
    data = await state.get_data()
    period = data.get("req_period", "?")
    await message.answer(
        f"<b>Замена на {esc(period)}</b>\n"
        "\n"
        f"Причина: <i>{esc(reason)}</i>\n"
        "\n"
        f"{SEP}\n"
        "<i>Кого предложить?</i>",
        reply_markup=keyboards.req_replace_candidate_choice_keyboard(),
    )


@router.callback_query(F.data == "req_replace_find")
async def on_req_replace_find(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DutyStates.waiting_request_candidate)
    await callback.message.edit_text(
        "<b>Поиск замены</b>\n"
        "\n"
        "<i>Введите имя/фамилию или @тег.</i>",
        reply_markup=keyboards.back_keyboard(),
    )
    await callback.answer()


@router.message(DutyStates.waiting_request_candidate)
async def on_req_replace_candidate_query(message: Message, state: FSMContext):
    query = security.sanitize_text(message.text)
    if not query:
        await message.answer("<i>Пустой ввод. Попробуйте ещё раз.</i>")
        return
    try:
        results = await database.search_engineers(query)
    except database.QueryTooLong as e:
        await message.answer(f"<i>{esc(e)}</i>")
        return
    # Exclude self
    eng = await database.get_engineer_by_user_id(message.from_user.id)
    if eng:
        results = [r for r in results if r["id"] != eng["id"]]
    if not results:
        await message.answer(
            "<i>Никого не найдено. Попробуйте ещё раз или нажмите «Назад».</i>",
            reply_markup=keyboards.back_keyboard(),
        )
        return
    await message.answer(
        "<b>Выберите замену</b>",
        reply_markup=keyboards.req_replace_candidates_keyboard(results[:15]),
    )


@router.callback_query(F.data.startswith("req_replace_pick:"))
async def on_req_replace_pick(callback: CallbackQuery, state: FSMContext):
    candidate_id = int(callback.data.split(":")[1])
    candidate = await database.get_engineer_by_id(candidate_id)
    if not candidate:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    if not candidate.get("user_id"):
        await callback.answer(
            f"{candidate['full_name']} не зарегистрирован в боте — выбрать его нельзя.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    original_id = data.get("req_eng_id")
    period = data.get("req_period")
    projects = data.get("req_projects", [])
    reason = data.get("req_reason", "")
    if not (original_id and period):
        await callback.answer("Контекст утерян. Начните заново через меню.", show_alert=True)
        await state.clear()
        return

    if candidate_id == original_id:
        await callback.answer("Нельзя предложить заменой самого себя.", show_alert=True)
        return

    original = await database.get_engineer_by_id(original_id)
    rep_id = await database.create_pending_replacement(
        original_engineer_id=original_id,
        replacement_engineer_id=candidate_id,
        period=period,
        reason=reason,
        status="pending",
    )

    projects_block = "\n".join(f"· {esc(p)}" for p in projects)
    text_for_candidate = (
        "<b>Просьба о замене</b>\n"
        "\n"
        f"<b>{esc(original['full_name'])}</b> просит вас подменить его на дежурстве.\n"
        "\n"
        f"Период: <b>{esc(period)}</b>\n"
        f"Проекты:\n{projects_block}\n"
        "\n"
        f"Причина: <i>{esc(reason)}</i>"
    )
    try:
        await bot.send_message(
            candidate["user_id"], text_for_candidate,
            reply_markup=keyboards.planned_replacement_response_keyboard(rep_id),
        )
    except Exception:
        logger.exception("Failed to deliver replacement request to candidate")
        await callback.answer("Не удалось отправить кандидату.", show_alert=True)
        await database.update_pending_replacement_status(rep_id, "declined")
        return

    await state.clear()
    await callback.message.edit_text(
        f"<b>Запрос отправлен</b>\n"
        "\n"
        f"{esc(candidate['full_name'])} получил вашу просьбу.\n"
        "\n"
        f"{SEP}\n"
        "<i>Я сообщу когда он ответит.</i>",
        reply_markup=keyboards.back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "req_replace_no_candidate")
async def on_req_replace_no_candidate(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    original_id = data.get("req_eng_id")
    period = data.get("req_period")
    projects = data.get("req_projects", [])
    reason = data.get("req_reason", "")
    if not (original_id and period):
        await callback.answer("Контекст утерян. Начните заново через меню.", show_alert=True)
        await state.clear()
        return

    original = await database.get_engineer_by_id(original_id)
    rep_id = await database.create_pending_replacement(
        original_engineer_id=original_id,
        replacement_engineer_id=None,
        period=period,
        reason=reason,
        status="admin_notified",
    )

    tag = (original.get("telegram_tag") if original else "") or "—"
    projects_block = "\n".join(f"· {esc(p)}" for p in projects)
    admin_text = (
        "<b>Запрос на замену</b>\n"
        "\n"
        f"<b>{esc(original['full_name'])}</b> ({esc(tag)}) просит найти замену.\n"
        "\n"
        f"Период: <b>{esc(period)}</b>\n"
        f"Проекты:\n{projects_block}\n"
        "\n"
        f"Причина: <i>{esc(reason)}</i>\n"
        f"Замена: <i>не предложена</i>"
    )
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID, admin_text,
                reply_markup=keyboards.planned_acknowledge_keyboard(rep_id),
            )
        except Exception:
            logger.warning("Failed to deliver admin request notification")
    else:
        logger.warning("ADMIN_ID is not set; replacement-request notification dropped")

    await state.clear()
    await callback.message.edit_text(
        "<b>Запрос отправлен</b>\n"
        "\n"
        "Администратор получит ваше сообщение.\n"
        "\n"
        f"{SEP}\n"
        "<i>Он сам подберёт кандидата.</i>",
        reply_markup=keyboards.back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("planned_accept:"))
async def on_planned_accept(callback: CallbackQuery):
    rep_id = int(callback.data.split(":")[1])
    rep = await database.get_pending_replacement(rep_id)
    if not rep:
        await callback.answer("Запрос не найден.", show_alert=True)
        return
    if rep["status"] != "pending":
        await callback.answer("Запрос уже обработан.", show_alert=True)
        return

    candidate = await database.get_engineer_by_user_id(callback.from_user.id)
    if not candidate or candidate["id"] != rep["replacement_engineer_id"]:
        await callback.answer("Это не ваш запрос.", show_alert=True)
        return

    await database.update_pending_replacement_status(rep_id, "accepted")
    original = await database.get_engineer_by_id(rep["original_engineer_id"])

    await callback.message.edit_text(
        (callback.message.html_text or "")
        + f"\n\n{SEP}\n<b>✓ Вы согласились</b>",
        reply_markup=None,
    )
    await callback.answer("Принято!")

    # Notify original
    if original and original.get("user_id"):
        try:
            await bot.send_message(
                original["user_id"],
                f"<b>Замена согласована</b>\n"
                "\n"
                f"{esc(candidate['full_name'])} согласился подменить вас "
                f"на период <b>{esc(rep['period'])}</b>.\n"
                "\n"
                f"{SEP}\n"
                "<i>Администратор уведомлён.</i>",
            )
        except Exception:
            pass

    # Notify admin
    await _notify_admin(
        "<b>Плановая замена согласована</b>\n"
        "\n"
        f"Период: <b>{esc(rep['period'])}</b>\n"
        f"Было: {esc(original['full_name']) if original else '?'}\n"
        f"Стало: {esc(candidate['full_name'])}\n"
        f"Причина: <i>{esc(rep['reason'] or '')}</i>"
    )


@router.callback_query(F.data.startswith("planned_decline:"))
async def on_planned_decline(callback: CallbackQuery):
    rep_id = int(callback.data.split(":")[1])
    rep = await database.get_pending_replacement(rep_id)
    if not rep:
        await callback.answer("Запрос не найден.", show_alert=True)
        return
    if rep["status"] != "pending":
        await callback.answer("Запрос уже обработан.", show_alert=True)
        return

    candidate = await database.get_engineer_by_user_id(callback.from_user.id)
    if not candidate or candidate["id"] != rep["replacement_engineer_id"]:
        await callback.answer("Это не ваш запрос.", show_alert=True)
        return

    await database.update_pending_replacement_status(rep_id, "declined")
    original = await database.get_engineer_by_id(rep["original_engineer_id"])

    await callback.message.edit_text(
        (callback.message.html_text or "")
        + f"\n\n{SEP}\n<b>✗ Вы отказались</b>",
        reply_markup=None,
    )
    await callback.answer()

    if original and original.get("user_id"):
        try:
            await bot.send_message(
                original["user_id"],
                f"<b>Замена отказалась</b>\n"
                "\n"
                f"{esc(candidate['full_name'])} не смог подменить вас "
                f"на период <b>{esc(rep['period'])}</b>.\n"
                "\n"
                f"{SEP}\n"
                "<i>Попробуйте предложить другого через «Запрос на замену».</i>",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("planned_ack:"))
async def on_planned_ack(callback: CallbackQuery):
    # No-op acknowledgement button for admin
    try:
        await callback.message.edit_text(
            (callback.message.html_text or "") + f"\n\n{SEP}\n<i>Принято к сведению</i>",
            reply_markup=None,
        )
    except Exception:
        pass
    await callback.answer()
