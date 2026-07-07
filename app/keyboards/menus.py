"""Главное меню, справка и базовые клавиатуры."""
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


HELP_TEXT_ADMIN = (
    "<b>Справка</b>\n"
    "\n"
    "<b>Управление дежурствами</b>\n"
    "\n"
    "<b>Запустить дежурство</b>\n"
    "<i>Выбрать период и разослать запросы инженерам.</i>\n"
    "\n"
    "<b>Пересоздать дежурство</b>\n"
    "<i>Сбросить активный опрос и начать заново.</i>\n"
    "\n"
    "<b>Создать график сейчас</b>\n"
    "<i>Выгрузить .xlsx со снимком текущего состояния опроса.</i>\n"
    "\n"
    "<b>Опубликовать график</b>\n"
    "<i>Разослать график-картинку (PNG) всем зарегистрированным.</i>\n"
    "\n"
    "<b>Посмотреть график</b>\n"
    "<i>Прислать себе актуальный график-картинку.</i>\n"
    "\n"
    "<b>Напомнить</b>\n"
    "<i>Разослать напоминание всем кто ещё не ответил в активном опросе.</i>\n"
    "\n"
    "<b>Дослать опрос</b>\n"
    "<i>Отправить опрос новым участникам (кому ранее не отправлялось).</i>\n"
    "\n"
    "<b>Отменить опрос</b>\n"
    "<i>Прекратить текущий опрос — все собранные ответы будут утрачены.</i>\n"
    "\n"
    "<b>Импорт данных</b>\n"
    "<i>Загрузить актуальный список инженеров из Excel.</i>\n"
    "\n"
    "——————\n"
    "\n"
    "<b>Управление пользователями</b>\n"
    "\n"
    "<b>Заявки</b>\n"
    "<i>Запросы пользователей на привязку/отвязку аккаунта — одобрить или отклонить.</i>\n"
    "\n"
    "<b>Привязанные аккаунты</b>\n"
    "<i>Кто из инженеров уже зарегистрирован в боте.</i>\n"
    "\n"
    "<b>Отвязать аккаунт</b>\n"
    "<i>Снять привязку Telegram с записи.</i>\n"
    "\n"
    "——————\n"
    "\n"
    "<b>Личные функции</b>\n"
    "\n"
    "<b>Мои дежурства</b>\n"
    "<i>Ваши дежурства на ближайшие 4 недели.</i>\n"
    "\n"
    "<b>Текущий опрос</b>\n"
    "<i>Статус активного опроса, если вы в нём участвуете.</i>\n"
    "\n"
    "<b>Запрос на замену</b>\n"
    "<i>Попросить кого-то подменить вас заранее.</i>\n"
    "\n"
    "<b>Контакты коллег</b>\n"
    "<i>Поиск телефона/почты/Telegram по базе.</i>\n"
    "\n"
    "<b>Мой профиль</b>\n"
    "<i>Ваши данные в системе.</i>\n"
    "\n"
    "<b>Сообщить об ошибке</b>\n"
    "<i>Отправить сообщение администратору (вам же).</i>\n"
    "\n"
    "——————\n"
    "\n"
    "<i>Скрытые команды:</i> <code>/addme</code> <code>/reset_bindings</code> <code>/unbind</code>"
)


HELP_TEXT_USER = (
    "<b>Справка</b>\n"
    "\n"
    "<b>Текущий опрос</b>\n"
    "<i>Статус активного опроса и кнопки подтверждения.</i>\n"
    "\n"
    "<b>Посмотреть график</b>\n"
    "<i>Актуальный график дежурств картинкой.</i>\n"
    "\n"
    "<b>Запрос на замену</b>\n"
    "<i>Попросить кого-то подменить вас заранее (отпуск, командировка).</i>\n"
    "\n"
    "<b>Контакты коллег</b>\n"
    "<i>Поиск телефона/почты/Telegram по базе.</i>\n"
    "\n"
    "<b>Мой профиль</b>\n"
    "<i>Ваши данные из базы.</i>\n"
    "\n"
    "<b>Сообщить об ошибке</b>\n"
    "<i>Отправить сообщение администратору.</i>\n"
    "\n"
    "<b>Отвязать аккаунт</b>\n"
    "<i>Снять привязку Telegram с записи.</i>"
)


def help_text(is_admin: bool) -> str:
    return HELP_TEXT_ADMIN if is_admin else HELP_TEXT_USER


def main_menu_keyboard(
    is_admin: bool,
    *,
    has_active_session: bool = False,
    pending_requests: int = 0,
) -> InlineKeyboardMarkup:
    """
    Build the main menu.
    `has_active_session`: admin-only; adds session-dependent buttons.
    `pending_requests`: admin-only; shown as a counter on the "Заявки" button.
    """
    builder = InlineKeyboardBuilder()
    if is_admin:
        builder.button(text="Запустить дежурство",   callback_data="menu:duty")
        builder.button(text="Пересоздать",           callback_data="menu:recreate")
        builder.button(text="Создать график сейчас", callback_data="menu:export_now")
        builder.button(text="Импорт данных",         callback_data="menu:import")
        if has_active_session:
            builder.button(text="Напомнить",          callback_data="menu:remind")
            builder.button(text="Дослать опрос",      callback_data="menu:resend")
            builder.button(text="Отправить персонально", callback_data="menu:send_personal")
            builder.button(text="Опубликовать график", callback_data="menu:publish")
            builder.button(text="Посмотреть график",  callback_data="menu:view_schedule")
            builder.button(text="Отменить опрос",     callback_data="menu:cancel_poll")
        requests_label = "Заявки" if pending_requests == 0 else f"Заявки ({pending_requests})"
        builder.button(text=requests_label,          callback_data="menu:requests")
        builder.button(text="Привязанные аккаунты",  callback_data="menu:linked_list:0")
        builder.button(text="Отвязать аккаунт",      callback_data="menu:unlink")
        builder.button(text="Мои дежурства",         callback_data="menu:my_duties")
        builder.button(text="Текущий опрос",         callback_data="menu:current_poll")
        builder.button(text="Запрос на замену",      callback_data="menu:req_replace")
        builder.button(text="Мой профиль",           callback_data="menu:profile")
        builder.button(text="Контакты коллег",       callback_data="menu:contacts")
        builder.button(text="Сообщить об ошибке",    callback_data="menu:report")
        builder.button(text="Помощь",                callback_data="menu:help")
    else:
        builder.button(text="Текущий опрос",      callback_data="menu:current_poll")
        if has_active_session:
            builder.button(text="Посмотреть график", callback_data="menu:view_schedule")
        builder.button(text="Запрос на замену",   callback_data="menu:req_replace")
        builder.button(text="Мой профиль",        callback_data="menu:profile")
        builder.button(text="Контакты коллег",    callback_data="menu:contacts")
        builder.button(text="Сообщить об ошибке", callback_data="menu:report")
        builder.button(text="Отвязать аккаунт",   callback_data="menu:unlink_self")
        builder.button(text="Помощь",             callback_data="menu:help")
    builder.adjust(2)
    return builder.as_markup()


def accounts_list_keyboard(
    page: int,
    total_pages: int,
    *,
    mode: str,  # "linked" or "unlinked"
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    base = "menu:linked_list" if mode == "linked" else "menu:unlinked_list"

    pager_row = 0
    if total_pages > 1:
        if page > 0:
            builder.button(text="← Назад",  callback_data=f"{base}:{page - 1}")
            pager_row += 1
        if page < total_pages - 1:
            builder.button(text="Вперёд →", callback_data=f"{base}:{page + 1}")
            pager_row += 1

    # Toggle between linked / unlinked views
    if mode == "linked":
        builder.button(text="Показать незарегистрированных", callback_data="menu:unlinked_list:0")
    else:
        builder.button(text="Показать привязанных", callback_data="menu:linked_list:0")

    builder.button(text="В главное меню", callback_data="menu:back")

    if pager_row == 2:
        builder.adjust(2, 1, 1)
    elif pager_row == 1:
        builder.adjust(1, 1, 1)
    else:
        builder.adjust(1, 1)
    return builder.as_markup()


def linked_list_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    return accounts_list_keyboard(page, total_pages, mode="linked")


def profile_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Сообщить об ошибке", callback_data="menu:report_profile")
    builder.button(text="Назад",              callback_data="menu:back")
    builder.adjust(2)
    return builder.as_markup()


def back_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="menu:back")
    return builder.as_markup()
