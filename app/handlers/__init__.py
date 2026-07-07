"""Роутеры доменов. Порядок включения фиксированный."""
from . import (  # noqa: F401  (errors регистрируется на dp напрямую)
    errors,
)
from . import (
    linking, accounts_admin, menu, poll_admin,
    current_poll, schedule_output, duty_responses, planned,
)

routers = [
    linking.router,
    accounts_admin.router,
    menu.router,
    poll_admin.router,
    current_poll.router,
    schedule_output.router,
    duty_responses.router,
    planned.router,
]
