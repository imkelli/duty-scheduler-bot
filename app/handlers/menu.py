"""Главное меню и личные функции: профиль, контакты, отчёты об ошибках."""
import logging
from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from app import keyboards
from app.db import database
from app.handlers.helpers import (
    SEP, _build_main_menu, _is_admin, esc, show_main_menu,
)
from app.loader import bot
from app.middlewares import security
from app.services import excel_parser
from app.services.notify import (
    _now_local,
)
from app.states import DutyStates
from config import ADMIN_ID, EXCEL_FILE

router = Router(name="menu")
logger = logging.getLogger(__name__)


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    await show_main_menu(message, "<b>Главное меню</b>")


@router.callback_query(F.data == "menu:help")
async def menu_help(callback: CallbackQuery):
    from aiogram.exceptions import TelegramBadRequest
    is_admin = _is_admin(callback.from_user.id)
    try:
        await callback.message.edit_text(
            keyboards.help_text(is_admin),
            reply_markup=await _build_main_menu(is_admin),
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data == "menu:back")
async def menu_back(callback: CallbackQuery, state: FSMContext):
    cur_state = await state.get_state()
    if cur_state:
        await state.clear()
    try:
        await callback.message.edit_text(
            "<b>Главное меню</b>",
            reply_markup=await _build_main_menu(_is_admin(callback.from_user.id)),
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "menu:my_duties")
async def menu_my_duties(callback: CallbackQuery):
    eng = await database.get_engineer_by_user_id(callback.from_user.id)
    if not eng:
        await callback.answer("Сначала привяжите аккаунт.", show_alert=True)
        return
    try:
        rows = excel_parser.get_user_duties(EXCEL_FILE, eng["full_name"], filter_weeks=3)
    except excel_parser.ExcelError as e:
        await callback.message.edit_text(
            f"<b>Ошибка чтения Excel</b>\n{e}",
            reply_markup=keyboards.back_keyboard(),
        )
        await callback.answer()
        return
    except Exception:
        logger.exception("my_duties read failed")
        await callback.answer("Ошибка чтения графика.", show_alert=True)
        return

    has_any = any(projects for _, projects in rows)
    if not rows or not has_any:
        text = (
            "<b>Мои ближайшие дежурства</b>\n"
            "\n"
            "<i>На ближайшие 4 недели дежурств нет.</i>"
        )
    else:
        parts = ["<b>Мои ближайшие дежурства</b>", ""]
        for label, projects in rows:
            parts.append(f"<b>{esc(label)}</b>")
            if projects:
                parts.extend(f"· {esc(p)}" for p in projects)
            else:
                parts.append("· нет дежурств")
            parts.append("")
        text = "\n".join(parts).rstrip()

    await callback.message.edit_text(text, reply_markup=keyboards.back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def menu_profile(callback: CallbackQuery):
    eng = await database.get_engineer_by_user_id(callback.from_user.id)
    if not eng:
        await callback.answer("Сначала привяжите аккаунт.", show_alert=True)
        return
    tag   = eng.get("telegram_tag") or "—"
    phone = eng.get("phone") or "—"
    email = eng.get("email") or "—"
    text = (
        "<b>Ваш профиль</b>\n"
        "\n"
        f"Имя: {esc(eng['full_name'])}\n"
        f"Telegram: <code>{esc(tag)}</code>\n"
        f"Телефон: <code>{esc(phone)}</code>\n"
        f"Почта: <code>{esc(email)}</code>\n"
        "\n"
        f"{SEP}\n"
        "<i>Если данные неверны — нажмите «Сообщить об ошибке».</i>"
    )
    await callback.message.edit_text(text, reply_markup=keyboards.profile_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("menu:report"))
async def menu_report(callback: CallbackQuery, state: FSMContext):
    # menu:report_profile — launched from "Мой профиль"; menu:report — from main menu
    source = "profile" if callback.data == "menu:report_profile" else "main"
    await state.set_state(DutyStates.waiting_error_report)
    await state.update_data(report_source=source)
    if source == "profile":
        prompt = (
            "<b>Ошибка в данных профиля</b>\n"
            "\n"
            "<i>Опишите что неверно в вашем профиле — сообщение вместе с "
            "текущими данными уйдёт администратору. Чтобы отменить — нажмите «Назад».</i>"
        )
    else:
        prompt = (
            "<b>Сообщить об ошибке</b>\n"
            "\n"
            "<i>Введите ваше сообщение текстом — оно будет переслано администратору. "
            "Чтобы отменить — нажмите «Назад».</i>"
        )
    await callback.message.edit_text(prompt, reply_markup=keyboards.back_keyboard())
    await callback.answer()


@router.message(DutyStates.waiting_error_report)
async def on_error_report_text(message: Message, state: FSMContext):
    text = security.sanitize_text(message.text, max_length=2000)
    if not text:
        await message.answer(
            "<i>Пустой ввод. Введите текст или нажмите «Назад» в предыдущем сообщении.</i>"
        )
        return
    data = await state.get_data()
    source = data.get("report_source", "main")
    await state.clear()

    eng = await database.get_engineer_by_user_id(message.from_user.id)
    if eng:
        sender = f"{eng['full_name']} ({eng.get('telegram_tag') or '@?'})"
    else:
        sender = f"@{message.from_user.username or '?'} (id {message.from_user.id})"

    now = _now_local()
    if source == "profile" and eng:
        tag   = eng.get("telegram_tag") or "не указан"
        phone = eng.get("phone") or "не указан"
        email = eng.get("email") or "не указана"
        admin_text = (
            "<b>Ошибка в данных профиля</b>\n"
            "\n"
            f"От: <b>{esc(sender)}</b>\n"
            "\n"
            "<b>Текущие данные пользователя:</b>\n"
            f"Имя: {esc(eng['full_name'])}\n"
            f"Telegram: {esc(tag)}\n"
            f"Телефон: {esc(phone)}\n"
            f"Почта: {esc(email)}\n"
            "\n"
            "<b>Сообщение пользователя:</b>\n"
            f"<i>\"{esc(text)}\"</i>\n"
            "\n"
            f"{SEP}\n"
            f"<i>Время: {now}</i>"
        )
    else:
        admin_text = (
            "<b>Сообщение об ошибке</b>\n"
            "\n"
            f"От: <b>{esc(sender)}</b>\n"
            "\n"
            "<b>Сообщение пользователя:</b>\n"
            f"<i>\"{esc(text)}\"</i>\n"
            "\n"
            f"{SEP}\n"
            f"<i>Время: {now}</i>"
        )

    delivered = False
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, admin_text)
            delivered = True
        except Exception:
            logger.exception("Failed to forward report to admin")
    else:
        logger.warning("ADMIN_ID not set; user report dropped")

    if delivered:
        await message.answer(
            "<b>Сообщение отправлено</b>\n"
            "\n"
            "Администратор получил вашу заявку и свяжется с вами при необходимости.",
            reply_markup=keyboards.back_keyboard(),
        )
    else:
        await message.answer(
            "<i>Не удалось доставить сообщение администратору. Попробуйте позже.</i>",
            reply_markup=keyboards.back_keyboard(),
        )


CONTACTS_MAX_RESULTS = 15


@router.callback_query(F.data == "menu:contacts")
async def menu_contacts(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DutyStates.waiting_contacts_query)
    await callback.message.edit_text(
        "<b>Контакты коллег</b>\n"
        "\n"
        "<i>Введите имя, фамилию или часть @тега для поиска.</i>\n"
        "\n"
        f"{SEP}\n"
        "<i>Например: «Иванов», «Иван», «@iv». "
        "Для нового поиска просто введите запрос снова.</i>",
        reply_markup=keyboards.back_keyboard(),
    )
    await callback.answer()


@router.message(DutyStates.waiting_contacts_query)
async def on_contacts_query(message: Message, state: FSMContext):
    query = security.sanitize_text(message.text)
    if not query:
        await message.answer("<i>Пустой ввод. Попробуйте ещё раз.</i>")
        return
    try:
        results = await database.search_engineers(query)
    except database.QueryTooLong as e:
        await message.answer(
            f"<i>{esc(e)}</i>",
            reply_markup=keyboards.back_keyboard(),
        )
        return

    if not results:
        await message.answer(
            "<i>Никого не найдено. Попробуйте другой запрос.</i>",
            reply_markup=keyboards.back_keyboard(),
        )
        return

    truncated = False
    if len(results) > CONTACTS_MAX_RESULTS:
        results = results[:CONTACTS_MAX_RESULTS]
        truncated = True

    lines = [f"<b>Найдено: {len(results)}{'+' if truncated else ''}</b>", ""]
    for e in results:
        tag = e.get("telegram_tag") or "тег отсутствует"
        phone = e.get("phone") or ""
        # email is not in search_engineers — fetch it
        full = await database.get_engineer_by_id(e["id"])
        email = (full or {}).get("email") or ""

        lines.append(f"<b>{esc(e['full_name'])}</b>")
        if tag and tag != "тег отсутствует":
            lines.append(f"Telegram: <code>{esc(tag)}</code>")
        else:
            lines.append("Telegram: <i>тег отсутствует</i>")
        if phone:
            lines.append(f"Телефон: <code>{esc(phone)}</code>")
        if email:
            lines.append(f"Почта: <code>{esc(email)}</code>")
        lines.append("")

    if truncated:
        lines.append(f"{SEP}")
        lines.append(f"<i>Показаны первые {CONTACTS_MAX_RESULTS}. Уточните запрос для более точного поиска.</i>")
    else:
        lines.append(f"{SEP}")
        lines.append("<i>Для нового поиска введите запрос снова или нажмите «Назад».</i>")

    await message.answer("\n".join(lines).rstrip(), reply_markup=keyboards.back_keyboard())
