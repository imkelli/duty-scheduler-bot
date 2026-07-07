"""Ответы дежурных и движок передачи проектов заменам."""
import logging
from aiogram import F
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from app import keyboards
from app.db import database
from app.handlers.helpers import (
    SEP, _abort_if_cancelled, _cancel_replacement_timeout, _check_session_complete, _latest_transfer_candidate, _name_tag, _nuke_other_messages, _project_names, _reject_already_answered, _reject_if_cancelled, esc,
)
from app.loader import bot
from app.middlewares import security
from app.services import scheduler
from app.states import DutyStates

router = Router(name="duty_responses")
logger = logging.getLogger(__name__)


MAX_PROJECT_TRANSFERS = 3


async def _engineer_pending_projects(session_id: int, engineer_id: int) -> list[dict]:
    rows = await database.get_projects_for_engineer(session_id, engineer_id)
    return [p for p in rows if p["status"] == database.AP_PENDING]


@router.callback_query(F.data.startswith("duty_confirm:"))
async def on_duty_confirm(callback: CallbackQuery, state: FSMContext):
    assignment_id = int(callback.data.split(":")[1])
    a = await database.get_assignment(assignment_id)
    if not a:
        await callback.answer("Задание не найдено.")
        return
    if await _reject_if_cancelled(callback, a["session_id"]):
        return
    eng = await database.get_engineer_by_user_id(callback.from_user.id)
    pending = await _engineer_pending_projects(a["session_id"], a["engineer_id"])
    if not pending:
        await _reject_already_answered(callback, "ответ уже дан")
        return

    await state.clear()
    _cancel_replacement_timeout(callback.from_user.id)
    await database.bulk_set_project_status(
        [p["id"] for p in pending], database.AP_CONFIRMED_SELF
    )
    await database.update_assignment_status(assignment_id, "confirmed")  # dual-write
    await callback.message.edit_text(
        (callback.message.html_text or "")
        + f"\n\n{SEP}\n<b>✓ Вы подтвердили дежурство</b>",
        reply_markup=None,
    )
    await callback.answer("Принято!")
    if eng:
        await _nuke_other_messages(
            assignment_id, eng["id"],
            except_message_id=callback.message.message_id,
            summary_text="Вы ответили: <b>Подтверждаю</b>", kind="duty",
        )
    await _check_session_complete(a["session_id"])


@router.callback_query(F.data.startswith("duty_decline:"))
async def on_duty_decline(callback: CallbackQuery, state: FSMContext):
    assignment_id = int(callback.data.split(":")[1])
    a = await database.get_assignment(assignment_id)
    if not a:
        await callback.answer("Задание не найдено.")
        return
    if await _reject_if_cancelled(callback, a["session_id"]):
        return
    eng = await database.get_engineer_by_user_id(callback.from_user.id)
    pending = await _engineer_pending_projects(a["session_id"], a["engineer_id"])
    if not pending:
        await _reject_already_answered(callback, "ответ уже дан")
        return

    await state.clear()
    _cancel_replacement_timeout(callback.from_user.id)
    await database.bulk_set_project_status(
        [p["id"] for p in pending], database.AP_DECLINED
    )
    await database.update_assignment_status(assignment_id, "declined")  # dual-write
    await callback.message.edit_text(
        (callback.message.html_text or "")
        + f"\n\n{SEP}\n<b>✗ Вы отказались от дежурства</b>",
        reply_markup=None,
    )
    await callback.answer()
    if eng:
        await _nuke_other_messages(
            assignment_id, eng["id"],
            except_message_id=callback.message.message_id,
            summary_text="Вы ответили: <b>Не подтверждаю</b>", kind="duty",
        )
    await _check_session_complete(a["session_id"])


async def _show_checklist(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pending = await _engineer_pending_projects(data["session_id"], data["engineer_id"])
    selected = set(data.get("selected", []))
    session = await database.get_duty_session(data["session_id"])
    period = session["period"] if session else "?"
    text = (
        "<b>Выберите проекты для передачи</b>\n"
        "\n"
        f"Дежурство: <code>{esc(period)}</code>\n"
        "\n"
        f"{SEP}\n"
        "<i>Отметьте проекты которые хотите передать одному человеку. "
        "Остальные сможете обработать дальше.</i>"
    )
    kb_projects = [{"id": p["id"], "project_name": p["project_name"]} for p in pending]
    try:
        await callback.message.edit_text(
            text, reply_markup=keyboards.project_checklist_keyboard(kb_projects, selected)
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("duty_replace:"))
async def on_duty_replace(callback: CallbackQuery, state: FSMContext):
    assignment_id = int(callback.data.split(":")[1])
    a = await database.get_assignment(assignment_id)
    if not a:
        await callback.answer("Задание не найдено.")
        return
    if await _reject_if_cancelled(callback, a["session_id"]):
        return
    pending = await _engineer_pending_projects(a["session_id"], a["engineer_id"])
    if not pending:
        await _reject_already_answered(callback, "ответ уже дан")
        return

    await state.set_state(DutyStates.replace_checklist)
    await state.update_data(
        assignment_id=assignment_id, session_id=a["session_id"],
        engineer_id=a["engineer_id"], selected=[],
    )
    await _show_checklist(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("chk_toggle:"), DutyStates.replace_checklist)
async def on_chk_toggle(callback: CallbackQuery, state: FSMContext):
    ap_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    if await _abort_if_cancelled(callback, state, data.get("session_id")):
        return
    selected = set(data.get("selected", []))
    if ap_id in selected:
        selected.discard(ap_id)
    else:
        selected.add(ap_id)
    await state.update_data(selected=list(selected))
    await _show_checklist(callback, state)
    await callback.answer()


async def _show_candidate_prompt(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    names = []
    for ap_id in data.get("selected", []):
        ap = await database.get_assignment_project(ap_id)
        if ap:
            names.append(ap["project_name"])
    block = "\n".join(f"· {esc(n)}" for n in names) or "· —"
    try:
        await callback.message.edit_text(
            "<b>Кому передать?</b>\n"
            "\n"
            f"Передаваемые проекты:\n{block}\n"
            "\n"
            f"{SEP}\n"
            "<i>Введите имя, фамилию или @тег замены. "
            "Поиск работает по всей базе инженеров.</i>",
            reply_markup=_chk_cancel_kb(),
        )
    except Exception:
        pass


def _chk_cancel_kb():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.button(text="Отмена", callback_data="chk_cancel")
    return b.as_markup()


@router.callback_query(F.data == "chk_confirm", DutyStates.replace_checklist)
async def on_chk_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if await _abort_if_cancelled(callback, state, data.get("session_id")):
        return
    if not data.get("selected"):
        await callback.answer("Отметьте хотя бы один проект.", show_alert=True)
        return
    await state.set_state(DutyStates.replace_candidate)
    await _show_candidate_prompt(callback, state)
    await callback.answer()


@router.callback_query(F.data == "chk_cancel")
async def on_chk_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    assignment_id = data.get("assignment_id")
    await state.clear()
    if assignment_id:
        a = await database.get_assignment(assignment_id)
        if a:
            session = await database.get_duty_session(a["session_id"])
            period = session["period"] if session else "?"
            pending = await _engineer_pending_projects(a["session_id"], a["engineer_id"])
            if pending:
                try:
                    await callback.message.edit_text(
                        scheduler.format_duty_text(period, [p["project_name"] for p in pending]),
                        reply_markup=keyboards.duty_confirm_keyboard(assignment_id),
                    )
                    await callback.answer()
                    return
                except Exception:
                    pass
    try:
        await callback.message.edit_text("<i>Передача отменена.</i>", reply_markup=None)
    except Exception:
        pass
    await callback.answer()


@router.message(DutyStates.replace_candidate)
async def on_transfer_candidate_input(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected", [])
    if not selected:
        await state.clear()
        return
    if await database.is_session_cancelled(data.get("session_id")):
        await state.clear()
        await message.answer(
            "<b>Опрос недействителен</b>\n"
            "\n"
            "<i>Опрос был отменён администратором. Поиск замены прекращён.</i>"
        )
        return
    query = security.sanitize_text(message.text)
    if not query:
        await message.answer("<i>Пустой ввод. Введите имя или @тег.</i>")
        return
    try:
        results = await database.search_engineers(query)
    except database.QueryTooLong as e:
        await message.answer(f"<i>{esc(e)}</i>")
        return
    # Exclude the initiator
    results = [r for r in results if r["id"] != data["engineer_id"]]
    # Exclude candidates who already declined ANY of the selected projects (loop guard)
    declined: set[int] = set()
    for ap_id in selected:
        declined |= await database.get_declined_candidates_for_project(ap_id)
    results = [r for r in results if r["id"] not in declined]
    if not results:
        await message.answer(
            "<i>Никого не найдено (или все варианты уже отказались по этим проектам). "
            "Попробуйте другой запрос.</i>"
        )
        return
    await message.answer(
        "<b>Выберите замену</b>",
        reply_markup=keyboards.transfer_candidates_keyboard(results[:15]),
    )


@router.callback_query(F.data.startswith("tr_pick:"), DutyStates.replace_candidate)
async def on_tr_pick(callback: CallbackQuery, state: FSMContext):
    cand_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    if await _abort_if_cancelled(callback, state, data.get("session_id")):
        return
    cand = await database.get_engineer_by_id(cand_id)
    if not cand:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    if not cand.get("user_id"):
        await callback.answer(
            f"{cand['full_name']} не зарегистрирован в боте — выбрать нельзя.",
            show_alert=True,
        )
        return
    data = await state.get_data()
    names = []
    for ap_id in data.get("selected", []):
        ap = await database.get_assignment_project(ap_id)
        if ap:
            names.append(ap["project_name"])
    await state.update_data(cand_id=cand_id)
    block = "\n".join(f"· {esc(n)}" for n in names) or "· —"
    try:
        await callback.message.edit_text(
            "<b>Подтверждение замены</b>\n"
            "\n"
            f"Кому: <b>{_name_tag(cand)}</b>\n"
            f"Передаваемые проекты:\n{block}",
            reply_markup=keyboards.transfer_confirm_keyboard(cand_id),
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("tr_send:"), DutyStates.replace_candidate)
async def on_tr_send(callback: CallbackQuery, state: FSMContext):
    cand_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    if await _abort_if_cancelled(callback, state, data.get("session_id")):
        return
    selected = list(data.get("selected", []))
    session_id = data.get("session_id")
    engineer_id = data.get("engineer_id")
    if not (selected and session_id and engineer_id):
        await callback.answer("Контекст утерян.", show_alert=True)
        await state.clear()
        return
    cand = await database.get_engineer_by_id(cand_id)
    if not cand or not cand.get("user_id"):
        await callback.answer("Кандидат недоступен.", show_alert=True)
        return

    req_id = await database.create_transfer_request(session_id, engineer_id, cand_id, selected)
    await database.bulk_set_project_status(selected, database.AP_TRANSFER_PENDING)
    security.get_logger().info(
        f"TRANSFER_SEND req={req_id} session={session_id} "
        f"initiator={engineer_id} candidate={cand_id} projects={selected}"
    )

    original = await database.get_engineer_by_id(engineer_id)
    session = await database.get_duty_session(session_id)
    period = session["period"] if session else "?"
    names = []
    for ap_id in selected:
        ap = await database.get_assignment_project(ap_id)
        if ap:
            names.append(ap["project_name"])
    block = "\n".join(f"· {esc(n)}" for n in names) or "· —"

    try:
        await bot.send_message(
            cand["user_id"],
            f"<b>Замена дежурства · <code>{esc(period)}</code></b>\n"
            "\n"
            f"<b>За кого:</b> {_name_tag(original)}\n"
            "\n"
            f"Проекты, которые предлагают принять:\n{block}\n"
            "\n"
            f"{SEP}\n"
            "<i>Принимаете дежурство?</i>",
            reply_markup=keyboards.transfer_response_keyboard(req_id),
        )
    except Exception:
        logger.exception("transfer request delivery failed")
        await callback.answer("Не удалось отправить кандидату.", show_alert=True)

    await callback.answer("Запрос отправлен")
    # Reset the selection, show what's left
    await state.update_data(selected=[], cand_id=None)
    await _show_remaining_or_finish(callback, state)


async def _show_remaining_or_finish(callback: CallbackQuery, state: FSMContext):
    """After a transfer is sent — show remaining pending projects, or the final summary."""
    data = await state.get_data()
    session_id = data["session_id"]
    engineer_id = data["engineer_id"]
    pending = await _engineer_pending_projects(session_id, engineer_id)
    if pending:
        await state.set_state(DutyStates.replace_remaining)
        block = "\n".join(f"· {esc(p['project_name'])}" for p in pending)
        try:
            await callback.message.edit_text(
                "<b>Запрос отправлен</b>\n"
                "\n"
                f"{SEP}\n"
                "<b>Оставшиеся проекты:</b>\n"
                f"{block}\n"
                "\n"
                "<i>Что сделать с ними?</i>",
                reply_markup=keyboards.remaining_projects_keyboard(),
            )
        except Exception:
            pass
    else:
        await state.clear()
        await _send_initiator_final_summary(callback.message.chat.id, session_id, engineer_id)
        try:
            await callback.message.edit_text(
                "<b>Запрос отправлен</b>\n\n<i>Все проекты обработаны.</i>",
                reply_markup=None,
            )
        except Exception:
            pass


async def _send_initiator_final_summary(chat_id: int, session_id: int, engineer_id: int):
    """The 'Ваши решения по дежурству' wrap-up for the initiator."""
    session = await database.get_duty_session(session_id)
    period = session["period"] if session else "?"
    projects = await database.get_projects_for_engineer(session_id, engineer_id)

    transferred, confirmed, declined = [], [], []
    for p in projects:
        if p["status"] in (database.AP_TRANSFER_PENDING, database.AP_TRANSFER_ACCEPTED):
            handler = await database.get_engineer_by_id(p["current_handler_id"])
            if p["status"] == database.AP_TRANSFER_ACCEPTED:
                mark = "принято ✓"
            else:
                mark = "ожидает ответа"
            # handler for pending is still the initiator — show the latest transfer candidate
            cand = handler
            if p["status"] == database.AP_TRANSFER_PENDING:
                cand = await _latest_transfer_candidate(session_id, p["id"])
            transferred.append(f"· {esc(p['project_name'])} → "
                               f"{esc(cand['full_name']) if cand else '?'} — {mark}")
        elif p["status"] == database.AP_CONFIRMED_SELF:
            confirmed.append(f"· {esc(p['project_name'])}")
        elif p["status"] == database.AP_DECLINED:
            declined.append(f"· {esc(p['project_name'])}")

    lines = [f"<b>Ваши решения по дежурству <code>{esc(period)}</code></b>", ""]
    if transferred:
        lines.append("<b>Переданы:</b>")
        lines.extend(transferred)
        lines.append("")
    if confirmed:
        lines.append("<b>Подтверждены за собой:</b>")
        lines.extend(confirmed)
        lines.append("")
    if declined:
        lines.append("<b>⚠️ Отказались (без дежурного):</b>")
        lines.extend(declined)
        lines.append("")
    lines.append(SEP)
    lines.append("<i>Вы получите уведомления когда замены ответят.</i>")
    try:
        await bot.send_message(chat_id, "\n".join(lines))
    except Exception:
        pass


@router.callback_query(F.data == "rem_transfer", DutyStates.replace_remaining)
async def on_rem_transfer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if await _abort_if_cancelled(callback, state, data.get("session_id")):
        return
    await state.set_state(DutyStates.replace_checklist)
    await state.update_data(selected=[])
    await _show_checklist(callback, state)
    await callback.answer()


@router.callback_query(F.data == "rem_confirm", DutyStates.replace_remaining)
async def on_rem_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if await _abort_if_cancelled(callback, state, data.get("session_id")):
        return
    pending = await _engineer_pending_projects(data["session_id"], data["engineer_id"])
    await database.bulk_set_project_status(
        [p["id"] for p in pending], database.AP_CONFIRMED_SELF
    )
    await state.clear()
    try:
        await callback.message.edit_text(
            "<b>Оставшиеся проекты подтверждены за вами.</b>", reply_markup=None
        )
    except Exception:
        pass
    await _send_initiator_final_summary(
        callback.message.chat.id, data["session_id"], data["engineer_id"]
    )
    await callback.answer()
    await _check_session_complete(data["session_id"])


@router.callback_query(F.data == "rem_decline", DutyStates.replace_remaining)
async def on_rem_decline(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if await _abort_if_cancelled(callback, state, data.get("session_id")):
        return
    pending = await _engineer_pending_projects(data["session_id"], data["engineer_id"])
    await database.bulk_set_project_status(
        [p["id"] for p in pending], database.AP_DECLINED
    )
    await state.clear()
    try:
        await callback.message.edit_text(
            "<b>Вы отказались от оставшихся проектов.</b>", reply_markup=None
        )
    except Exception:
        pass
    await _send_initiator_final_summary(
        callback.message.chat.id, data["session_id"], data["engineer_id"]
    )
    await callback.answer()
    await _check_session_complete(data["session_id"])


@router.callback_query(F.data.startswith("tr_accept:"))
async def on_tr_accept(callback: CallbackQuery):
    req_id = int(callback.data.split(":")[1])
    req = await database.get_transfer_request(req_id)
    if not req:
        await callback.answer("Запрос не найден.", show_alert=True)
        return
    if await database.is_session_cancelled(req["session_id"]):
        await _reject_if_cancelled(callback, req["session_id"])
        return
    if req["status"] != "pending":
        await _reject_already_answered(callback, "запрос уже обработан")
        return
    cand = await database.get_engineer_by_user_id(callback.from_user.id)
    if not cand or cand["id"] != req["candidate_engineer_id"]:
        await callback.answer("Это не ваш запрос.", show_alert=True)
        return

    await database.update_transfer_request_status(req_id, "accepted")
    await database.bulk_set_project_status(
        req["project_ids"], database.AP_TRANSFER_ACCEPTED, current_handler_id=cand["id"]
    )
    names = await _project_names(req["project_ids"])
    block = "\n".join(f"· {esc(n)}" for n in names) or "· —"
    original = await database.get_engineer_by_id(req["initiator_engineer_id"])

    await callback.message.edit_text(
        (callback.message.html_text or "")
        + f"\n\n{SEP}\n<b>✓ Вы согласились подменить дежурного</b>\n"
        + f"<b>За кого:</b> {_name_tag(original)}\n"
        + f"<b>Принятые проекты:</b>\n{block}",
        reply_markup=None,
    )
    await callback.answer("Принято!")
    if original and original.get("user_id"):
        lines = "\n".join(
            f"· {esc(n)} → {_name_tag(cand)} ✓" for n in names
        )
        try:
            await bot.send_message(
                original["user_id"],
                f"<b>Замена принята</b>\n\n{lines}",
            )
        except Exception:
            pass
    await _check_session_complete(req["session_id"])


@router.callback_query(F.data.startswith("tr_decline:"))
async def on_tr_decline(callback: CallbackQuery):
    req_id = int(callback.data.split(":")[1])
    req = await database.get_transfer_request(req_id)
    if not req:
        await callback.answer("Запрос не найден.", show_alert=True)
        return
    if await database.is_session_cancelled(req["session_id"]):
        await _reject_if_cancelled(callback, req["session_id"])
        return
    if req["status"] != "pending":
        await _reject_already_answered(callback, "запрос уже обработан")
        return
    cand = await database.get_engineer_by_user_id(callback.from_user.id)
    if not cand or cand["id"] != req["candidate_engineer_id"]:
        await callback.answer("Это не ваш запрос.", show_alert=True)
        return

    await database.update_transfer_request_status(req_id, "declined")
    # Each project: status → transfer_rejected, decline counter +1
    all_at_limit = True
    for ap_id in req["project_ids"]:
        ap = await database.get_assignment_project(ap_id)
        if not ap:
            continue
        new_count = ap["replacement_chain_count"] + 1
        await database.update_assignment_project(
            ap_id, status=database.AP_TRANSFER_REJECTED,
            replacement_chain_count=new_count,
        )
        if new_count < MAX_PROJECT_TRANSFERS:
            all_at_limit = False

    names = await _project_names(req["project_ids"])
    block = "\n".join(f"· {esc(n)}" for n in names) or "· —"
    await callback.message.edit_text(
        (callback.message.html_text or "")
        + f"\n\n{SEP}\n<b>✗ Вы отказались от замены</b>\nПроекты:\n{block}",
        reply_markup=None,
    )
    await callback.answer()

    original = await database.get_engineer_by_id(req["initiator_engineer_id"])
    if original and original.get("user_id"):
        try:
            await bot.send_message(
                original["user_id"],
                "<b>Замена отказалась</b>\n"
                "\n"
                f"<b>Кто отказался:</b> {_name_tag(cand)}\n"
                f"<b>По проектам:</b>\n{block}\n"
                "\n"
                f"{SEP}\n"
                "<i>Что сделать с этими проектами?</i>",
                reply_markup=keyboards.declined_transfer_options_keyboard(
                    req_id, can_transfer=not all_at_limit
                ),
            )
        except Exception:
            pass
    security.get_logger().info(
        f"TRANSFER_DECLINED req={req_id} candidate={cand['id']} all_at_limit={all_at_limit}"
    )


@router.callback_query(F.data.startswith("dec_confirm:"))
async def on_dec_confirm(callback: CallbackQuery):
    req_id = int(callback.data.split(":")[1])
    req = await database.get_transfer_request(req_id)
    if not req:
        await callback.answer("Запрос не найден.", show_alert=True)
        return
    if await _reject_if_cancelled(callback, req["session_id"]):
        return
    await database.bulk_set_project_status(
        req["project_ids"], database.AP_CONFIRMED_SELF,
        current_handler_id=req["initiator_engineer_id"],
    )
    await callback.message.edit_text(
        (callback.message.html_text or "")
        + f"\n\n{SEP}\n<b>✓ Проекты подтверждены за вами</b>",
        reply_markup=None,
    )
    await callback.answer()
    await _check_session_complete(req["session_id"])


@router.callback_query(F.data.startswith("dec_decline:"))
async def on_dec_decline(callback: CallbackQuery):
    req_id = int(callback.data.split(":")[1])
    req = await database.get_transfer_request(req_id)
    if not req:
        await callback.answer("Запрос не найден.", show_alert=True)
        return
    if await _reject_if_cancelled(callback, req["session_id"]):
        return
    await database.bulk_set_project_status(req["project_ids"], database.AP_DECLINED)
    await callback.message.edit_text(
        (callback.message.html_text or "")
        + f"\n\n{SEP}\n<b>✗ Проекты помечены как без дежурного</b>",
        reply_markup=None,
    )
    await callback.answer()
    await _check_session_complete(req["session_id"])


@router.callback_query(F.data.startswith("dec_transfer:"))
async def on_dec_transfer(callback: CallbackQuery, state: FSMContext):
    req_id = int(callback.data.split(":")[1])
    req = await database.get_transfer_request(req_id)
    if not req:
        await callback.answer("Запрос не найден.", show_alert=True)
        return
    if await _abort_if_cancelled(callback, state, req["session_id"]):
        return
    # Re-transfer only projects still under the decline limit
    retriable = []
    for ap_id in req["project_ids"]:
        ap = await database.get_assignment_project(ap_id)
        if ap and ap["replacement_chain_count"] < MAX_PROJECT_TRANSFERS:
            retriable.append(ap_id)
    if not retriable:
        await callback.answer("Лимит передач по этим проектам исчерпан.", show_alert=True)
        return
    await state.set_state(DutyStates.replace_candidate)
    await state.update_data(
        assignment_id=None, session_id=req["session_id"],
        engineer_id=req["initiator_engineer_id"], selected=retriable,
    )
    await _show_candidate_prompt(callback, state)
    await callback.answer()
