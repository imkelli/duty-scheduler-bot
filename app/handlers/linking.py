"""Регистрация через /start и заявки на привязку/отвязку."""
from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from typing import Optional
from app import keyboards
from app.db import database
from app.handlers.helpers import (
    SEP, _check_admin, _remove_reply_keyboard, esc, show_main_menu,
)
from app.middlewares import security
from app.services.notify import (
    _notify_admin, _notify_user_safe, _now_local,
)
from app.states import DutyStates
from config import ADMIN_ID

router = Router(name="linking")


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user = message.from_user
    uid = user.id

    # Admin: full access, straight to the menu
    if uid == ADMIN_ID:
        await state.clear()
        await show_main_menu(message, "<b>Главное меню</b>")
        return

    # Already linked → just show the menu
    eng = await database.get_engineer_by_user_id(uid)
    if eng:
        await state.clear()
        await show_main_menu(message, "<b>Главное меню</b>")
        return

    # Already has an open request → remind, don't create a duplicate
    pending = await database.get_pending_request_by_user(uid)
    if pending:
        await _remove_reply_keyboard(message)
        await message.answer(
            "<b>Ожидание подтверждения</b>\n"
            "\n"
            "<i>Ваш запрос на привязку ещё рассматривается администратором. "
            "Пожалуйста, дождитесь решения.</i>"
        )
        return

    if not user.username:
        await _remove_reply_keyboard(message)
        await message.answer(
            "<b>Username не задан</b>\n"
            "\n"
            "<i>Установите username в настройках Telegram и попробуйте снова.</i>"
        )
        return

    # Start the link search flow
    security.add_link_flow_user(uid)
    await state.set_state(DutyStates.waiting_link_query)
    await _remove_reply_keyboard(message)
    await message.answer(
        "<b>Привязка аккаунта</b>\n"
        "\n"
        "Введите ваш <b>@тег</b> или <b>имя и фамилию</b> для поиска в базе.\n"
        "\n"
        f"{SEP}\n"
        "<i>Например: «Иванов Иван» или «@ivanov»</i>",
        reply_markup=keyboards.linkreq_cancel_keyboard(),
    )


@router.message(DutyStates.waiting_link_query)
async def on_link_query(message: Message, state: FSMContext):
    uid = message.from_user.id
    query = security.sanitize_text(message.text)
    if not query:
        await message.answer("<i>Пустой ввод. Введите @тег или имя для поиска.</i>")
        return
    try:
        results = await database.search_engineers(query)
    except database.QueryTooLong as e:
        await message.answer(f"<i>{esc(e)}</i>")
        return
    if not results:
        await message.answer(
            "<i>Запись не найдена. Попробуйте другой запрос или нажмите «Отмена».</i>",
            reply_markup=keyboards.linkreq_cancel_keyboard(),
        )
        return
    await message.answer(
        "<b>Найдите свою запись</b>\n"
        "\n"
        "<i>Выберите запись, к которой нужно привязать ваш Telegram:</i>",
        reply_markup=keyboards.linkreq_candidates_keyboard(results[:15]),
    )


@router.callback_query(F.data == "linkreq_cancel")
async def on_linkreq_cancel(callback: CallbackQuery, state: FSMContext):
    security.remove_link_flow_user(callback.from_user.id)
    await state.clear()
    try:
        await callback.message.edit_text(
            "<i>Привязка отменена. Чтобы начать заново — введите /start.</i>",
            reply_markup=None,
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("linkreq_pick:"))
async def on_linkreq_pick(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    record_id = int(callback.data.split(":")[1])
    eng_record = await database.get_engineer_by_id(record_id)
    if not eng_record:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    # Guard against duplicate requests
    if await database.get_pending_request_by_user(uid):
        security.remove_link_flow_user(uid)
        await state.clear()
        await callback.message.edit_text(
            "<b>Ожидание подтверждения</b>\n"
            "\n"
            "<i>У вас уже есть запрос на рассмотрении.</i>",
            reply_markup=None,
        )
        await callback.answer()
        return
    if await database.get_engineer_by_user_id(uid):
        security.remove_link_flow_user(uid)
        await state.clear()
        await callback.message.edit_text(
            "<i>Ваш аккаунт уже привязан. Введите /menu.</i>", reply_markup=None,
        )
        await callback.answer()
        return

    proposed_tag = f"@{callback.from_user.username}" if callback.from_user.username else "—"
    req_id = await database.create_pending_request(
        uid, "link", record_id, proposed_tag=proposed_tag,
    )
    security.remove_link_flow_user(uid)
    await state.clear()
    security.get_logger().info(
        f"LINK_REQUEST req={req_id} user={security.mask_user_id(uid)} record={record_id}"
    )

    # Notify the admin
    phone = eng_record.get("phone") or "—"
    base_tag = eng_record.get("telegram_tag") or "—"
    await _notify_admin(
        "<b>Запрос на привязку аккаунта</b>\n"
        "\n"
        "<b>Найденная запись:</b>\n"
        f"Имя: {esc(eng_record['full_name'])}\n"
        f"Telegram в базе: {esc(base_tag)}\n"
        f"Телефон: {esc(phone)}\n"
        "\n"
        "<b>Запрашивает:</b>\n"
        f"Telegram пользователя: {esc(proposed_tag)}\n"
        f"User ID: <code>{uid}</code>\n"
        "\n"
        f"{SEP}\n"
        f"<i>Время: {_now_local()}</i>",
        reply_markup=keyboards.request_decision_keyboard(req_id),
    )

    await callback.message.edit_text(
        "<b>Запрос отправлен</b>\n"
        "\n"
        "Ваш запрос на привязку аккаунта отправлен администратору.\n"
        "Ожидайте подтверждения.\n"
        "\n"
        f"{SEP}\n"
        "<i>Вы получите уведомление как только администратор примет решение.</i>",
        reply_markup=None,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("req_approve:"))
async def on_req_approve(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action="req_approve"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    req_id = int(callback.data.split(":")[1])
    req = await database.get_pending_request(req_id)
    if not req:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if req["status"] != "pending":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return

    target = await database.get_engineer_by_id(req["target_record_id"]) if req["target_record_id"] else None
    target_name = target["full_name"] if target else "?"

    if req["request_type"] == "link":
        if req["target_record_id"]:
            await database.link_user_id_by_id(req["target_record_id"], req["user_id"])
        await database.resolve_pending_request(req_id, "approved")
        security.get_logger().info(
            f"REQ_APPROVE_LINK req={req_id} user={security.mask_user_id(req['user_id'])} "
            f"record={req['target_record_id']}"
        )
        user_menu = keyboards.main_menu_keyboard(False)
        await _notify_user_safe(
            req["user_id"],
            "<b>Аккаунт привязан</b>\n"
            "\n"
            "Администратор одобрил ваш запрос. Теперь вам доступны все функции бота.",
            reply_markup=user_menu,
        )
        admin_done = f"<b>✓ Привязка одобрена</b>\n\n{esc(target_name)}"
    else:  # unlink
        if req["target_record_id"]:
            await database.unlink_user_id(req["target_record_id"])
        await database.resolve_pending_request(req_id, "approved")
        security.get_logger().info(
            f"REQ_APPROVE_UNLINK req={req_id} user={security.mask_user_id(req['user_id'])} "
            f"record={req['target_record_id']}"
        )
        await _notify_user_safe(
            req["user_id"],
            "<b>Аккаунт отвязан</b>\n"
            "\n"
            "Администратор одобрил отвязку. Для повторного использования бота "
            "потребуется заново привязать аккаунт через команду /start.",
        )
        admin_done = f"<b>✓ Отвязка одобрена</b>\n\n{esc(target_name)}"

    try:
        await callback.message.edit_text(
            (callback.message.html_text or "") + f"\n\n{SEP}\n{admin_done}",
            reply_markup=None,
        )
    except Exception:
        pass
    await callback.answer("Одобрено")


@router.callback_query(F.data.startswith("req_reject:"))
async def on_req_reject(callback: CallbackQuery, state: FSMContext):
    if not _check_admin(callback.from_user.id, action="req_reject"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    req_id = int(callback.data.split(":")[1])
    req = await database.get_pending_request(req_id)
    if not req:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if req["status"] != "pending":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    await state.set_state(DutyStates.waiting_reject_reason)
    await state.update_data(reject_req_id=req_id)
    await callback.message.answer(
        "<b>Отклонение заявки</b>\n"
        "\n"
        "<i>Введите причину отклонения текстом — она будет показана пользователю. "
        "Либо нажмите «Без причины».</i>",
        reply_markup=keyboards.reject_reason_keyboard(req_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("req_reject_nocomment:"))
async def on_req_reject_nocomment(callback: CallbackQuery, state: FSMContext):
    if not _check_admin(callback.from_user.id, action="req_reject_nocomment"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    req_id = int(callback.data.split(":")[1])
    await state.clear()
    await _finalize_rejection(req_id, None)
    try:
        await callback.message.edit_text(
            (callback.message.html_text or "") + f"\n\n{SEP}\n<b>✗ Заявка отклонена</b>",
            reply_markup=None,
        )
    except Exception:
        pass
    await callback.answer("Отклонено")


@router.message(DutyStates.waiting_reject_reason)
async def on_reject_reason_text(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id, action="reject_reason"):
        await state.clear()
        return
    data = await state.get_data()
    req_id = data.get("reject_req_id")
    await state.clear()
    if not req_id:
        return
    comment = security.sanitize_text(message.text, max_length=500)
    await _finalize_rejection(req_id, comment or None)
    await message.answer("<b>✗ Заявка отклонена</b>")


async def _finalize_rejection(req_id: int, comment: Optional[str]):
    req = await database.get_pending_request(req_id)
    if not req or req["status"] != "pending":
        return
    await database.resolve_pending_request(req_id, "rejected", comment)
    security.get_logger().info(
        f"REQ_REJECT req={req_id} type={req['request_type']} "
        f"user={security.mask_user_id(req['user_id'])}"
    )
    reason_line = f"\nПричина: <i>{esc(comment)}</i>" if comment else ""
    if req["request_type"] == "link":
        await _notify_user_safe(
            req["user_id"],
            "<b>Запрос отклонён</b>\n"
            "\n"
            f"Администратор отклонил ваш запрос на привязку аккаунта.{reason_line}\n"
            "\n"
            f"{SEP}\n"
            f"<i>Для уточнений обратитесь к {security.admin_mention()}.</i>",
        )
    else:  # unlink rejected — user stays linked, gets the menu back
        await _notify_user_safe(
            req["user_id"],
            "<b>Запрос на отвязку отклонён</b>\n"
            "\n"
            f"Администратор отклонил вашу заявку. Ваш аккаунт остаётся привязанным.{reason_line}",
            reply_markup=keyboards.main_menu_keyboard(False),
        )


@router.callback_query(F.data == "menu:requests")
async def menu_requests(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action="menu:requests"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    requests = await database.get_all_pending_requests()
    if not requests:
        try:
            await callback.message.edit_text(
                "<b>Ожидающие заявки</b>\n"
                "\n"
                "<i>Ожидающих заявок нет.</i>",
                reply_markup=keyboards.back_keyboard(),
            )
        except Exception:
            pass
        await callback.answer()
        return

    all_eng = await database.get_all_engineers()
    by_id = {e["id"]: e for e in all_eng}

    lines = [f"<b>Ожидающие заявки</b> · всего: <b>{len(requests)}</b>", ""]
    for i, r in enumerate(requests, start=1):
        target = by_id.get(r["target_record_id"])
        target_name = target["full_name"] if target else "?"
        if r["request_type"] == "link":
            lines.append(
                f"{i}. <b>Привязка #{r['id']}</b> — "
                f"{esc(r['proposed_tag'] or '—')} → {esc(target_name)}"
            )
        else:
            tag = (target.get("telegram_tag") if target else "") or "—"
            lines.append(
                f"{i}. <b>Отвязка #{r['id']}</b> — "
                f"{esc(target_name)} ({esc(tag)})"
            )
    try:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=keyboards.requests_list_keyboard(requests),
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "menu:unlink_self")
async def menu_unlink_self(callback: CallbackQuery):
    eng = await database.get_engineer_by_user_id(callback.from_user.id)
    if not eng:
        await callback.answer("Ваш аккаунт не привязан.", show_alert=True)
        return
    if await database.get_pending_request_by_user(callback.from_user.id):
        await callback.answer("У вас уже есть запрос на рассмотрении.", show_alert=True)
        return
    await callback.message.edit_text(
        "<b>Отвязать аккаунт?</b>\n"
        "\n"
        "После отвязки вам будут недоступны функции бота до повторной привязки "
        "и одобрения администратором.",
        reply_markup=keyboards.confirm_unlink_self_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "unlink_self_confirm")
async def on_unlink_self_confirm(callback: CallbackQuery):
    uid = callback.from_user.id
    eng = await database.get_engineer_by_user_id(uid)
    if not eng:
        await callback.answer("Ваш аккаунт не привязан.", show_alert=True)
        return
    if await database.get_pending_request_by_user(uid):
        await callback.answer("У вас уже есть запрос на рассмотрении.", show_alert=True)
        return

    req_id = await database.create_pending_request(uid, "unlink", eng["id"])
    security.get_logger().info(
        f"UNLINK_REQUEST req={req_id} user={security.mask_user_id(uid)} engineer_id={eng['id']}"
    )

    tag = eng.get("telegram_tag") or "—"
    await _notify_admin(
        "<b>Запрос на отвязку аккаунта</b>\n"
        "\n"
        "<b>Пользователь:</b>\n"
        f"Имя: {esc(eng['full_name'])}\n"
        f"Telegram: {esc(tag)}\n"
        f"User ID: <code>{uid}</code>\n"
        "\n"
        f"{SEP}\n"
        f"<i>Время: {_now_local()}</i>",
        reply_markup=keyboards.request_decision_keyboard(req_id),
    )
    await callback.message.edit_text(
        "<b>Запрос отправлен</b>\n"
        "\n"
        "Ваш запрос на отвязку аккаунта отправлен администратору.\n"
        "Ожидайте подтверждения.",
        reply_markup=None,
    )
    await callback.answer()
