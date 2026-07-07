"""Админские клавиатуры: периоды, подтверждения, публикация."""
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def personal_candidates_keyboard(engineers: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for e in engineers:
        tag = e.get("telegram_tag") or "тег отсутствует"
        builder.button(
            text=f"{e['full_name']} · {tag}",
            callback_data=f"personal_pick:{e['id']}",
        )
    builder.button(text="Отмена", callback_data="menu:back")
    builder.adjust(1)
    return builder.as_markup()


def personal_confirm_keyboard(engineer_id: int, mode: str) -> InlineKeyboardMarkup:
    """mode ∈ {'force', 'resend'} — confirm sending despite a warning."""
    builder = InlineKeyboardBuilder()
    if mode == "resend":
        builder.button(
            text="Сбросить статус и отправить заново",
            callback_data=f"personal_send:{engineer_id}:resend",
        )
    else:
        builder.button(text="Да, отправить", callback_data=f"personal_send:{engineer_id}:{mode}")
    builder.button(text="Отмена", callback_data="menu:back")
    builder.adjust(1)
    return builder.as_markup()


def confirm_recreate_keyboard(session_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Сбросить", callback_data=f"recreate_confirm:{session_id}")
    builder.button(text="Отмена",   callback_data="recreate_cancel")
    builder.adjust(2)
    return builder.as_markup()


def confirm_publish_keyboard(session_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Опубликовать", callback_data=f"publish_confirm:{session_id}")
    builder.button(text="Отмена",       callback_data="menu:back")
    builder.adjust(2)
    return builder.as_markup()


def confirm_cancel_poll_keyboard(session_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, отменить",  callback_data=f"cancel_poll_confirm:{session_id}")
    builder.button(text="Нет, оставить", callback_data="cancel_poll_cancel")
    builder.adjust(2)
    return builder.as_markup()


def admin_current_poll_keyboard(
    session_id: int,
    resettable: list[tuple[int, str]] | None = None,
) -> InlineKeyboardMarkup:
    """
    resettable: list of (assignment_id, short_name) for people who already
    answered — each gets a '🔄 <name>' one-tap reset button.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="Обновить",              callback_data="menu:current_poll")
    builder.button(text="Напомнить",             callback_data="menu:remind")
    builder.button(text="Создать график сейчас", callback_data="menu:export_now")
    builder.button(text="Отменить опрос",        callback_data="menu:cancel_poll")
    builder.button(text="В главное меню",        callback_data="menu:back")

    reset_buttons = resettable or []
    for assignment_id, name in reset_buttons:
        builder.button(text=f"🔄 {name}", callback_data=f"poll_reset:{assignment_id}")

    # layout: 2,2,1 for the controls, then reset buttons 2-per-row
    layout = [2, 2, 1]
    remaining = len(reset_buttons)
    while remaining > 0:
        layout.append(2 if remaining >= 2 else 1)
        remaining -= 2
    builder.adjust(*layout)
    return builder.as_markup()


def periods_keyboard(periods: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for col_index, label in periods:
        builder.button(text=label, callback_data=f"period:{col_index}:{label}")
    builder.adjust(2)
    return builder.as_markup()


def finalize_keyboard(session_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Создать график",   callback_data=f"create_schedule:{session_id}")
    builder.button(text="Не создавать",     callback_data=f"skip_schedule:{session_id}")
    builder.adjust(2)
    return builder.as_markup()
