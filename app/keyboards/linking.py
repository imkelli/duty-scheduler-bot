"""Клавиатуры привязки/отвязки аккаунтов и заявок."""
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def linkreq_candidates_keyboard(engineers: list[dict]) -> InlineKeyboardMarkup:
    """Search results when a user picks their own record for a link request."""
    builder = InlineKeyboardBuilder()
    for e in engineers:
        tag = e.get("telegram_tag") or "тег отсутствует"
        builder.button(
            text=f"{e['full_name']} · {tag}",
            callback_data=f"linkreq_pick:{e['id']}",
        )
    builder.button(text="Отмена", callback_data="linkreq_cancel")
    builder.adjust(1)
    return builder.as_markup()


def linkreq_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Отмена", callback_data="linkreq_cancel")
    return builder.as_markup()


def confirm_unlink_self_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, отвязать", callback_data="unlink_self_confirm")
    builder.button(text="Отмена",       callback_data="menu:back")
    builder.adjust(2)
    return builder.as_markup()


def request_decision_keyboard(req_id: int) -> InlineKeyboardMarkup:
    """Approve / reject buttons attached to an admin notification about a request."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Одобрить",  callback_data=f"req_approve:{req_id}")
    builder.button(text="Отклонить", callback_data=f"req_reject:{req_id}")
    builder.adjust(2)
    return builder.as_markup()


def reject_reason_keyboard(req_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Без причины", callback_data=f"req_reject_nocomment:{req_id}")
    builder.adjust(1)
    return builder.as_markup()


def requests_list_keyboard(requests: list[dict]) -> InlineKeyboardMarkup:
    """One Approve/Reject pair per pending request, plus a back button."""
    builder = InlineKeyboardBuilder()
    layout: list[int] = []
    for r in requests:
        builder.button(text=f"✓ Заявка #{r['id']}", callback_data=f"req_approve:{r['id']}")
        builder.button(text=f"✗ Заявка #{r['id']}", callback_data=f"req_reject:{r['id']}")
        layout.append(2)
    builder.button(text="В главное меню", callback_data="menu:back")
    layout.append(1)
    builder.adjust(*layout)
    return builder.as_markup()


def addme_keyboard(engineers: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for eng in engineers:
        tag = eng["telegram_tag"] or "нет тега"
        builder.button(text=f"{eng['full_name']} · {tag}", callback_data=f"addme_pick:{eng['id']}")
    builder.adjust(1)
    return builder.as_markup()


def unlink_keyboard(engineers: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for eng in engineers:
        tag = eng["telegram_tag"] or "нет тега"
        builder.button(text=f"{eng['full_name']} · {tag}", callback_data=f"unlink_pick:{eng['id']}")
    builder.adjust(1)
    return builder.as_markup()
