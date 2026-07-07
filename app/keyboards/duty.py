"""Клавиатуры опроса, передачи проектов и плановых замен."""
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def project_checklist_keyboard(projects: list[dict], selected: set[int]) -> InlineKeyboardMarkup:
    """
    projects: list of {id, project_name} — projects available to transfer.
    selected: set of assignment_projects ids currently ticked.
    """
    builder = InlineKeyboardBuilder()
    for p in projects:
        mark = "☑" if p["id"] in selected else "☐"
        builder.button(text=f"{mark} {p['project_name']}",
                       callback_data=f"chk_toggle:{p['id']}")
    if selected:
        builder.button(text="Подтвердить выбор", callback_data="chk_confirm")
    builder.button(text="Отмена", callback_data="chk_cancel")
    builder.adjust(1)
    return builder.as_markup()


def transfer_candidates_keyboard(engineers: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for e in engineers:
        tag = e.get("telegram_tag") or "нет тега"
        marker = "" if e.get("user_id") else " · нет /start"
        builder.button(text=f"{e['full_name']} · {tag}{marker}",
                       callback_data=f"tr_pick:{e['id']}")
    builder.button(text="Отмена", callback_data="chk_cancel")
    builder.adjust(1)
    return builder.as_markup()


def transfer_confirm_keyboard(candidate_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Отправить запрос", callback_data=f"tr_send:{candidate_id}")
    builder.button(text="Отмена",           callback_data="chk_cancel")
    builder.adjust(1)
    return builder.as_markup()


def transfer_response_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Sent to the candidate — accept / decline the whole transfer request."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✓ Принять",    callback_data=f"tr_accept:{request_id}")
    builder.button(text="✗ Отказаться", callback_data=f"tr_decline:{request_id}")
    builder.adjust(2)
    return builder.as_markup()


def remaining_projects_keyboard() -> InlineKeyboardMarkup:
    """Shown to the initiator after sending one transfer — what to do with the rest."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Передать ещё кому-то",          callback_data="rem_transfer")
    builder.button(text="Подтвердить оставшееся за собой", callback_data="rem_confirm")
    builder.button(text="Отказаться от оставшихся",      callback_data="rem_decline")
    builder.adjust(1)
    return builder.as_markup()


def declined_transfer_options_keyboard(request_id: int, can_transfer: bool) -> InlineKeyboardMarkup:
    """
    Shown to the initiator when a candidate declined. `can_transfer` is False
    when every project in the request has hit the 3-decline limit.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="Подтвердить за собой", callback_data=f"dec_confirm:{request_id}")
    if can_transfer:
        builder.button(text="Передать другому", callback_data=f"dec_transfer:{request_id}")
    builder.button(text="Отказаться полностью", callback_data=f"dec_decline:{request_id}")
    builder.adjust(1)
    return builder.as_markup()


def req_replace_weeks_keyboard(weeks: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """weeks = [(col_index, label), ...] — periods where the user is on duty."""
    builder = InlineKeyboardBuilder()
    for col, label in weeks:
        builder.button(text=label, callback_data=f"req_replace_week:{col}:{label}")
    builder.button(text="Назад", callback_data="menu:back")
    builder.adjust(1)
    return builder.as_markup()


def req_replace_candidate_choice_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Найти замену",        callback_data="req_replace_find")
    builder.button(text="Без конкретной замены", callback_data="req_replace_no_candidate")
    builder.button(text="Отмена",              callback_data="menu:back")
    builder.adjust(1)
    return builder.as_markup()


def req_replace_candidates_keyboard(engineers: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for e in engineers:
        tag = e.get("telegram_tag") or "нет тега"
        marker = "" if e.get("user_id") else " · нет /start"
        builder.button(
            text=f"{e['full_name']} · {tag}{marker}",
            callback_data=f"req_replace_pick:{e['id']}",
        )
    builder.button(text="Отмена", callback_data="menu:back")
    builder.adjust(1)
    return builder.as_markup()


def planned_replacement_response_keyboard(rep_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✓ Согласен",  callback_data=f"planned_accept:{rep_id}")
    builder.button(text="✗ Отказаться", callback_data=f"planned_decline:{rep_id}")
    builder.adjust(2)
    return builder.as_markup()


def planned_acknowledge_keyboard(rep_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Принять к сведению", callback_data=f"planned_ack:{rep_id}")
    return builder.as_markup()


def duty_confirm_keyboard(assignment_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✓ Подтвердить", callback_data=f"duty_confirm:{assignment_id}")
    builder.button(text="✗ Отказаться",  callback_data=f"duty_decline:{assignment_id}")
    builder.button(text="→ Замена",      callback_data=f"duty_replace:{assignment_id}")
    builder.adjust(2, 1)
    return builder.as_markup()
