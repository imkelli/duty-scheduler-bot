"""FSM-состояния бота."""
from aiogram.fsm.state import State, StatesGroup


class DutyStates(StatesGroup):
    waiting_addme_query = State()
    waiting_unlink_query = State()
    waiting_error_report = State()
    waiting_contacts_query = State()
    waiting_request_reason = State()
    waiting_request_candidate = State()
    waiting_personal_query = State()
    waiting_link_query = State()
    waiting_reject_reason = State()
    # --- new replacement engine (per-project) ---
    replace_checklist = State()
    replace_candidate = State()
    replace_remaining = State()
