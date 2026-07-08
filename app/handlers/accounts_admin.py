"""Админ-управление аккаунтами: привязки, списки, импорт данных."""
import logging
import os
from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from app import keyboards
from app.db import database
from app.handlers.helpers import (
    SEP, _build_main_menu, _check_admin, _ordered_gap, _safe_search, esc,
)
from app.loader import bot
from app.middlewares import security
from app.services import excel_parser
from app.services.notify import (
    _notify_admin_link, _refresh_admin_tag,
)
from app.states import DutyStates
from config import ADMIN_ID, EXCEL_FILE

router = Router(name="accounts_admin")
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "menu:import")
async def menu_import(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await _ordered_gap()
    await _do_import(callback.message.chat.id)


@router.callback_query(F.data == "menu:addme")
async def menu_addme(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _ordered_gap()
    await _do_addme(callback.from_user.id, callback.message.chat.id, state)


@router.callback_query(F.data == "menu:unlink")
async def menu_unlink(callback: CallbackQuery, state: FSMContext):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(DutyStates.waiting_unlink_query)
    await callback.message.edit_text(
        "<b>Отвязать аккаунт</b>\n"
        "\n"
        "<i>Введите имя и фамилию или Telegram-тег (с <code>@</code>):</i>"
    )
    await callback.answer()


@router.message(DutyStates.waiting_unlink_query)
async def on_unlink_query(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id, action="fsm_input"):
        await state.clear()
        return
    query = security.sanitize_text(message.text)
    if not query:
        await message.answer("<i>Пустой ввод. Попробуйте ещё раз:</i>")
        return
    results = await _safe_search(message, query, linked_only=True)
    if results is None:
        return
    if not results:
        await message.answer("<i>Привязанных аккаунтов не найдено. Попробуйте ещё раз:</i>")
        return
    await state.clear()
    await message.answer(
        "<b>Выберите запись для отвязки</b>",
        reply_markup=keyboards.unlink_keyboard(results),
    )


@router.callback_query(F.data.startswith("unlink_pick:"))
async def on_unlink_pick(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    engineer_id = int(callback.data.split(":")[1])
    eng = await database.get_engineer_by_id(engineer_id)
    if not eng:
        await callback.answer("Запись не найдена.")
        return
    await database.unlink_user_id(engineer_id)
    tag = eng["telegram_tag"] or "нет тега"
    security.get_logger().info(f"UNLINK engineer_id={engineer_id} by={security.mask_user_id(callback.from_user.id)}")
    await callback.message.edit_text(
        f"<b>✓ Аккаунт отвязан</b>\n"
        "\n"
        f"{esc(eng['full_name'])} · <code>{esc(tag)}</code>"
    )
    await callback.answer()


@router.callback_query(F.data == "menu:reset_bindings")
async def menu_reset_bindings(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action=callback.data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    cleared = await database.reset_all_bindings_except(ADMIN_ID)
    security.get_logger().warning(
        f"RESET_BINDINGS by={security.mask_user_id(callback.from_user.id)} cleared={cleared}"
    )
    await callback.answer(f"Сброшено: {cleared}")
    await _ordered_gap()
    await callback.message.edit_text(
        "<b>✓ Привязки сброшены</b>\n"
        "\n"
        f"Очищено записей: <b>{cleared}</b>\n"
        f"Ваша привязка (admin) сохранена.\n"
        "\n"
        f"{SEP}\n"
        "<i>Каждый инженер должен заново сделать /start, чтобы привязать свой Telegram.</i>",
        reply_markup=await _build_main_menu(True),
    )


LINKED_LIST_PAGE_SIZE = 30


async def _render_accounts_list(callback: CallbackQuery, mode: str, page: int):
    """mode ∈ {'linked', 'unlinked'} — admin paginated view of engineers."""
    all_eng = await database.get_all_engineers()
    linked_count = sum(1 for e in all_eng if e.get("user_id") is not None)
    unlinked_count = len(all_eng) - linked_count

    if mode == "linked":
        items = [e for e in all_eng if e.get("user_id") is not None]
        title = "Привязанные аккаунты"
        footer = f"<i>Не зарегистрированы: {unlinked_count}</i>"
        empty_text = "<i>Никто ещё не зарегистрирован через /start.</i>"
    else:
        items = [e for e in all_eng if e.get("user_id") is None]
        title = "Незарегистрированные"
        footer = f"<i>Привязаны: {linked_count}</i>"
        empty_text = "<i>Все записи в базе уже привязаны.</i>"

    items.sort(key=lambda e: (e["full_name"] or "").lower())
    total = len(items)

    if total == 0:
        text = (
            f"<b>{title}</b> · всего: <b>0</b>\n"
            "\n"
            f"{empty_text}\n"
            "\n"
            f"{SEP}\n"
            f"{footer}"
        )
        try:
            await callback.message.edit_text(
                text,
                reply_markup=keyboards.accounts_list_keyboard(0, 1, mode=mode),
            )
        except Exception:
            pass
        await callback.answer()
        return

    total_pages = (total + LINKED_LIST_PAGE_SIZE - 1) // LINKED_LIST_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start = page * LINKED_LIST_PAGE_SIZE
    chunk = items[start:start + LINKED_LIST_PAGE_SIZE]

    header = f"<b>{title}</b> · всего: <b>{total}</b>"
    if total_pages > 1:
        header += f"  ·  стр. {page + 1}/{total_pages}"

    body_lines = []
    for i, e in enumerate(chunk, start=start + 1):
        tag = e.get("telegram_tag") or "тег отсутствует"
        body_lines.append(f"{i}. {esc(e['full_name'])} · <code>{esc(tag)}</code>")

    text = (
        f"{header}\n"
        "\n"
        + "\n".join(body_lines)
        + "\n\n"
        f"{SEP}\n"
        f"{footer}"
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=keyboards.accounts_list_keyboard(page, total_pages, mode=mode),
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("menu:linked_list:"))
async def menu_linked_list(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action="menu:linked_list"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        page = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        page = 0
    await _render_accounts_list(callback, "linked", page)


@router.callback_query(F.data.startswith("menu:unlinked_list:"))
async def menu_unlinked_list(callback: CallbackQuery):
    if not _check_admin(callback.from_user.id, action="menu:unlinked_list"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        page = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        page = 0
    await _render_accounts_list(callback, "unlinked", page)


async def _do_import(chat_id: int):
    if not os.path.exists(EXCEL_FILE):
        await bot.send_message(chat_id, f"<b>Ошибка</b>\nФайл <code>{esc(EXCEL_FILE)}</code> не найден.")
        return
    backup = security.backup_database()
    if backup:
        logger.info(f"Pre-import backup created: {backup.name}")
    try:
        upserted, ambiguous = await excel_parser.import_phones(EXCEL_FILE)
        await _refresh_admin_tag()  # admin tag may have changed in the new file
        text = (
            "<b>✓ Импорт завершён</b>\n"
            f"<i>Обновлено записей: {upserted}.</i>"
        )
        if ambiguous:
            names_block = "\n".join(f"· {esc(n)}" for n in sorted(ambiguous))
            text += (
                "\n\n<b>⚠️ Тёзки — пропущены, нужно ручное уточнение</b>\n"
                "<i>В базе несколько записей с таким ФИО; автоматическое "
                "обновление могло бы склеить разных людей:</i>\n"
                f"<pre>{names_block}</pre>"
            )
        await bot.send_message(chat_id, text)
    except excel_parser.ExcelError as e:
        await bot.send_message(chat_id, f"<b>Ошибка импорта</b>\n{e}")
    except ValueError as e:
        # Validation errors from security.validate_excel — safe to show
        await bot.send_message(chat_id, f"<b>Ошибка импорта</b>\n<i>{esc(e)}</i>")
    except Exception as e:
        # Unexpected errors — log full trace, show neutral message
        logger.exception("Import failed")
        await bot.send_message(chat_id, "<b>Ошибка импорта</b>\n<i>Подробности см. в логах.</i>")


@router.message(Command("import"))
async def cmd_import(message: Message):
    if not _check_admin(message.from_user.id, action="cmd"):
        return
    await _do_import(message.chat.id)


@router.message(Command("reset_bindings"))
async def cmd_reset_bindings(message: Message):
    if not _check_admin(message.from_user.id, action="reset_bindings"):
        return
    cleared = await database.reset_all_bindings_except(ADMIN_ID)
    security.get_logger().warning(
        f"RESET_BINDINGS by={security.mask_user_id(message.from_user.id)} cleared={cleared}"
    )
    await message.answer(
        "<b>✓ Привязки сброшены</b>\n"
        "\n"
        f"Очищено записей: <b>{cleared}</b>\n"
        f"Ваша привязка (admin) сохранена.\n"
        "\n"
        f"{SEP}\n"
        "<i>Каждый инженер должен заново сделать /start, чтобы привязать свой Telegram.</i>"
    )


@router.message(Command("unbind"))
async def cmd_unbind(message: Message):
    if not _check_admin(message.from_user.id, action="unbind"):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "<b>Использование</b>\n"
            "\n"
            "<code>/unbind &lt;имя или @тег&gt;</code>\n"
            "\n"
            "<i>Пример: <code>/unbind Иванов</code> или <code>/unbind @ivanov</code></i>"
        )
        return
    query = security.sanitize_text(parts[1])
    results = await _safe_search(message, query, linked_only=True)
    if results is None:
        return
    if not results:
        await message.answer(
            f"<i>По запросу <code>{esc(query)}</code> не найдено привязанных записей.</i>"
        )
        return
    if len(results) > 1:
        # Multiple matches — fall back to picker keyboard
        await message.answer(
            "<b>Найдено несколько записей</b>\n"
            "\n"
            "<i>Выберите кого отвязать:</i>",
            reply_markup=keyboards.unlink_keyboard(results),
        )
        return
    eng = results[0]
    await database.unlink_user_id(eng["id"])
    security.get_logger().info(
        f"UNBIND engineer_id={eng['id']} by={security.mask_user_id(message.from_user.id)} "
        f"name={eng['full_name']!r}"
    )
    tag = eng["telegram_tag"] or "нет тега"
    await message.answer(
        f"<b>✓ Аккаунт отвязан</b>\n"
        "\n"
        f"{esc(eng['full_name'])} · <code>{esc(tag)}</code>"
    )


async def _do_addme(user_id: int, chat_id: int, state: FSMContext):
    await state.update_data(addme_user_id=user_id)
    await state.set_state(DutyStates.waiting_addme_query)
    await bot.send_message(
        chat_id,
        "<b>Привязать аккаунт</b>\n"
        "\n"
        "<i>Эта кнопка привязывает <b>ваш</b> Telegram к выбранной записи. "
        "Не нажимайте на чужие записи — иначе их привязка перепишется на вас.</i>\n"
        "\n"
        "<i>Введите имя и фамилию или Telegram-тег (с <code>@</code>):</i>",
    )


@router.message(Command("addme"))
async def cmd_addme(message: Message, state: FSMContext):
    await _do_addme(message.from_user.id, message.chat.id, state)


@router.message(DutyStates.waiting_addme_query)
async def on_addme_query(message: Message, state: FSMContext):
    query = security.sanitize_text(message.text)
    if not query:
        await message.answer("<i>Пустой ввод. Попробуйте ещё раз:</i>")
        return
    results = await _safe_search(message, query, linked_only=False)
    if results is None:
        return
    if not results:
        await message.answer("<i>Никого не найдено. Попробуйте ещё раз:</i>")
        return
    await state.clear()
    await message.answer(
        "<b>Выберите запись для привязки</b>",
        reply_markup=keyboards.addme_keyboard(results),
    )


@router.callback_query(F.data.startswith("addme_pick:"))
async def on_addme_pick(callback: CallbackQuery):
    engineer_id = int(callback.data.split(":")[1])
    eng = await database.get_engineer_by_id(engineer_id)
    if not eng:
        await callback.answer("Запись не найдена.")
        return

    user_id = callback.from_user.id
    # Defence: check if this user_id is already linked to a DIFFERENT engineer record.
    # If so, refuse — prevents admin (or anyone) from accidentally rebinding.
    existing = await database.get_engineer_by_user_id(user_id)
    if existing and existing["id"] != engineer_id:
        security.get_logger().warning(
            f"LINK_REFUSED user={security.mask_user_id(user_id)} "
            f"already_linked_to={existing['id']}({existing['full_name']!r}) "
            f"attempted_target={engineer_id}({eng['full_name']!r})"
        )
        await callback.answer(
            f"Ваш аккаунт уже привязан к записи: {existing['full_name']}.\n"
            f"Если нужно перепривязать — сначала отвяжите её.",
            show_alert=True,
        )
        await callback.message.edit_text(
            f"<b>Привязка отклонена</b>\n"
            "\n"
            f"Ваш аккаунт уже привязан к: <b>{esc(existing['full_name'])}</b>\n"
            "\n"
            f"{SEP}\n"
            "<i>Сначала отвяжите старую запись (для админа — кнопка «Отвязать аккаунт»).</i>"
        )
        return

    was_fresh = existing is None  # captured before update
    await database.link_user_id_by_id(engineer_id, user_id)
    tag = eng["telegram_tag"] or "нет тега"
    security.get_logger().info(f"LINK engineer_id={engineer_id} user={security.mask_user_id(user_id)}")
    if was_fresh:
        await _notify_admin_link(eng, callback.from_user, source="addme")
    await callback.message.edit_text(
        f"<b>✓ Аккаунт привязан</b>\n"
        "\n"
        f"{esc(eng['full_name'])} · <code>{esc(tag)}</code>"
    )
    await callback.answer()
