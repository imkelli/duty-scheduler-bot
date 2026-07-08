"""Точка входа: middlewares, роутеры, запуск polling."""
import asyncio

from aiogram.types import BotCommand, BotCommandScopeDefault

import config
from app.db import database
from app.handlers import routers
from app.loader import bot, dp
from app.middlewares import security
from app.services.notify import _refresh_admin_tag

# Security middlewares — на message и callback_query
_rate_limit_mw = security.RateLimitMiddleware()
_auth_mw = security.AuthMiddleware(admin_id=config.ADMIN_ID)
dp.message.middleware(_rate_limit_mw)
dp.message.middleware(_auth_mw)
dp.callback_query.middleware(_rate_limit_mw)
dp.callback_query.middleware(_auth_mw)

for _router in routers:
    dp.include_router(_router)


async def main():
    await database.init_db()
    await _refresh_admin_tag()
    # В меню Telegram видны только /start и /menu; админские команды скрыты.
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать работу с ботом"),
            BotCommand(command="menu",  description="Главное меню"),
        ],
        scope=BotCommandScopeDefault(),
    )
    try:
        await dp.start_polling(bot)
    finally:
        # Shutdown-хук: корректно закрыть общее соединение с БД
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
