"""Сводки «Текущий опрос» для администратора и участника."""
from aiogram import F
from aiogram import Router
from aiogram.types import CallbackQuery
from typing import Optional
from app import keyboards
from app.db import database
from app.handlers.helpers import (
    SEP, _check_admin, _is_admin, _latest_transfer_candidate, _name_tag, _project_names, _reset_assignment_and_resend, esc,
)

router = Router(name="current_poll")


@router.callback_query(F.data == "menu:current_poll")
async def menu_current_poll(callback: CallbackQuery):
    is_admin = _is_admin(callback.from_user.id)
    if is_admin:
        await _render_admin_current_poll(callback)
        return
    await _render_user_current_poll(callback)


async def _render_user_current_poll(callback: CallbackQuery):
    eng = await database.get_engineer_by_user_id(callback.from_user.id)
    if not eng:
        await callback.answer("Сначала привяжите аккаунт.", show_alert=True)
        return
    eng_id = eng["id"]
    session = await database.get_active_session()
    if not session:
        await callback.message.edit_text(
            "<b>Текущий опрос</b>\n\n<i>Сейчас активных опросов для вас нет.</i>",
            reply_markup=keyboards.back_keyboard(),
        )
        await callback.answer()
        return
    session_id = session["id"]
    period = session["period"]

    own = await database.get_projects_for_engineer(session_id, eng_id)
    all_aps = await database.get_session_assignment_projects(session_id)
    accepted_for_me = [
        ap for ap in all_aps
        if ap["engineer_id"] != eng_id
        and ap["current_handler_id"] == eng_id
        and ap["status"] == database.AP_TRANSFER_ACCEPTED
    ]
    incoming = await database.get_transfer_requests_for_candidate(
        session_id, eng_id, "pending"
    )

    if not own and not accepted_for_me and not incoming:
        await callback.message.edit_text(
            "<b>Текущий опрос</b>\n\n<i>Сейчас активных опросов для вас нет.</i>",
            reply_markup=keyboards.back_keyboard(),
        )
        await callback.answer()
        return

    parts: list[str] = [f"<b>Текущий опрос · <code>{esc(period)}</code></b>", ""]

    has_pending_own = False
    if own:
        parts.append("<b>Ваши проекты:</b>")
        for ap in own:
            st = ap["status"]
            proj = esc(ap["project_name"])
            if st == database.AP_PENDING:
                has_pending_own = True
                parts.append(f"· {proj} — <i>ожидает ответа</i>")
            elif st == database.AP_CONFIRMED_SELF:
                parts.append(f"· {proj} — подтверждено за вами ✓")
            elif st == database.AP_DECLINED:
                parts.append(f"· {proj} — вы отказались ✗")
            elif st == database.AP_TRANSFER_PENDING:
                cand = await _latest_transfer_candidate(session_id, ap["id"])
                parts.append(f"· {proj} → {_name_tag(cand)} — ожидает ответа")
            elif st == database.AP_TRANSFER_ACCEPTED:
                handler = await database.get_engineer_by_id(ap["current_handler_id"])
                parts.append(f"· {proj} → {_name_tag(handler)} — принято ✓")
            elif st == database.AP_TRANSFER_REJECTED:
                parts.append(f"· {proj} — замена отказалась, ожидается ваше решение")
            elif st == database.AP_NO_CONTACT:
                parts.append(f"· {proj} — нет связи")
            else:
                parts.append(f"· {proj} — {esc(st)}")
        parts.append("")

    if accepted_for_me:
        parts.append("<b>Проекты, которые вы приняли:</b>")
        for ap in accepted_for_me:
            orig = await database.get_engineer_by_id(ap["engineer_id"])
            parts.append(f"· {esc(ap['project_name'])} — за {_name_tag(orig)}")
        parts.append("")

    if incoming:
        parts.append("<b>Запросы на замену вам:</b>")
        for req in incoming:
            initiator = await database.get_engineer_by_id(req["initiator_engineer_id"])
            names = await _project_names(req["project_ids"])
            proj_str = ", ".join(esc(n) for n in names)
            parts.append(f"· от {_name_tag(initiator)}: {proj_str}")
        parts.append("<i>Ответьте в сообщении с запросом замены.</i>")
        parts.append("")

    parts.append(SEP)
    parts.append("<i>Чтобы обновить — нажмите «Текущий опрос» снова.</i>")

    kb = keyboards.back_keyboard()
    if has_pending_own:
        legacy = await database.get_session_assignments(session_id)
        a_id = next((a["id"] for a in legacy if a["engineer_id"] == eng_id), None)
        if a_id is not None:
            kb = keyboards.duty_confirm_keyboard(a_id)

    try:
        await callback.message.edit_text("\n".join(parts), reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


def _build_admin_poll_text(
    aps: list[dict],
    by_id: dict,
    period: str,
    latest_cand_map: dict,
    assignment_by_eng: dict,
) -> tuple[list[str], list[tuple[int, str]]]:
    """
    Pure renderer for the admin «Текущий опрос».

    Правило: каждая запись assignment_projects попадает РОВНО в один блок —
    по своему полю status. Пустые блоки не выводятся. Одна запись = одна
    строка вида «· Имя · @тег · Проект1, Проект2 · Статус».

    Returns (lines, resettable).
    """
    def _nm(eid):
        e = by_id.get(eid)
        return e["full_name"] if e else "?"

    # Один словарь на каждый статус — пересечений между блоками нет.
    confirmed_by_eng: dict[int, list[str]] = {}    # AP_CONFIRMED_SELF
    accepted_by_pair: dict[tuple, list[str]] = {}  # AP_TRANSFER_ACCEPTED
    transfer_by_pair: dict[tuple, list[str]] = {}  # AP_TRANSFER_PENDING
    rejected_by_eng: dict[int, list[str]] = {}     # AP_TRANSFER_REJECTED
    declined_by_eng: dict[int, list[str]] = {}     # AP_DECLINED
    waiting_by_eng: dict[int, list[str]] = {}      # AP_PENDING + есть user_id
    skipped_by_eng: dict[int, list[str]] = {}      # AP_NO_CONTACT / AP_PENDING без user_id
    engineers_with_progress: set[int] = set()

    for ap in aps:
        proj = ap["project_name"]
        eng_id = ap["engineer_id"]
        st = ap["status"]
        if st != database.AP_PENDING:
            engineers_with_progress.add(eng_id)
        if st == database.AP_CONFIRMED_SELF:
            confirmed_by_eng.setdefault(eng_id, []).append(proj)
        elif st == database.AP_TRANSFER_ACCEPTED:
            accepted_by_pair.setdefault((eng_id, ap["current_handler_id"]), []).append(proj)
        elif st == database.AP_TRANSFER_PENDING:
            cand = latest_cand_map.get(ap["id"])
            transfer_by_pair.setdefault(
                (eng_id, cand["id"] if cand else None), []
            ).append(proj)
        elif st == database.AP_TRANSFER_REJECTED:
            rejected_by_eng.setdefault(eng_id, []).append(proj)
        elif st == database.AP_DECLINED:
            declined_by_eng.setdefault(eng_id, []).append(proj)
        elif st == database.AP_NO_CONTACT:
            skipped_by_eng.setdefault(eng_id, []).append(proj)
        else:  # AP_PENDING
            if (by_id.get(eng_id) or {}).get("user_id"):
                waiting_by_eng.setdefault(eng_id, []).append(proj)
            else:
                skipped_by_eng.setdefault(eng_id, []).append(proj)

    resettable: list[tuple[int, str]] = []
    for eng_id in engineers_with_progress:
        a_id = assignment_by_eng.get(eng_id)
        if a_id is None:
            continue
        name = _nm(eng_id)
        resettable.append((a_id, name.split()[0] if name else "?"))
    resettable.sort(key=lambda t: t[1].lower())

    def _count(d):
        return sum(len(v) for v in d.values())

    total = len(aps)
    n_closed   = _count(confirmed_by_eng) + _count(accepted_by_pair)
    n_progress = _count(transfer_by_pair) + _count(rejected_by_eng)
    n_waiting  = _count(waiting_by_eng)
    n_no_duty  = _count(declined_by_eng) + _count(skipped_by_eng)

    # ── Шапка + табличная статистика в <pre> (моноширинно, копируется) ──
    def _stat(label: str, value: int) -> str:
        return f"{label:<16}{value:>3}"

    lines = [
        f"<b>Текущий опрос · <code>{esc(period)}</code></b>",
        "",
        "<pre>"
        + _stat("Проектов:",       total)      + "\n"
        + _stat("Закрыто:",        n_closed)   + "\n"
        + _stat("В процессе:",     n_progress) + "\n"
        + _stat("Ожидают:",        n_waiting)  + "\n"
        + _stat("Без дежурного:",  n_no_duty)
        + "</pre>",
    ]

    def _tag_of(eid) -> str:
        """@тег инженера, или пусто если тега нет ('-' / '—' / пусто)."""
        tag = ((by_id.get(eid) or {}).get("telegram_tag") or "").strip()
        return "" if tag in ("", "-", "—", "–") else tag

    def _surname(eid) -> str:
        name = _nm(eid)
        return name.split()[0] if name else "?"

    def _row(eid, projs: list[str], status: str) -> str:
        """Одна строка: '· Имя · @тег · Проект1, Проект2 · Статус'.

        Все проекты целиком (без сокращений). Поле @тег пропускается,
        если тега нет. Экранируется для вставки в <pre>.
        """
        parts = [esc(_nm(eid))]
        tag = _tag_of(eid)
        if tag:
            parts.append(esc(tag))
        parts.append(esc(", ".join(projs)))
        parts.append(esc(status))
        return "· " + " · ".join(parts)

    def _emit(title: str, body: list[str]):
        """Заголовок <b> + тело блока в <pre> (моноширинно, копируется)."""
        lines.append("")
        lines.append(f"<b>{title}</b>")
        lines.append("<pre>" + "\n".join(body) + "</pre>")

    def _simple_block(title: str, groups: dict, status: str):
        """Блок с фиксированным статусом: одна строка на инженера."""
        if not groups:
            return
        _emit(title, [
            _row(eid, projs, status)
            for eid, projs in sorted(groups.items(), key=lambda kv: _nm(kv[0]).lower())
        ])

    def _accepted_block(title: str, groups: dict):
        """Принятые передачи — две строки: инициатор и новый исполнитель."""
        if not groups:
            return
        body = []
        for (orig_id, handler_id), projs in sorted(
            groups.items(), key=lambda kv: _nm(kv[0][0]).lower()
        ):
            body.append(_row(orig_id, projs, f"Передано {_surname(handler_id)}"))
            body.append(_row(handler_id, projs, f"Принято от {_surname(orig_id)}"))
        _emit(title, body)

    def _pending_transfer_block(title: str, groups: dict):
        """Передачи в процессе — строка инициатора со статусом ожидания."""
        if not groups:
            return
        body = []
        for (orig_id, cand_id), projs in sorted(
            groups.items(), key=lambda kv: _nm(kv[0][0]).lower()
        ):
            who = _surname(cand_id) if cand_id else "замены"
            body.append(_row(orig_id, projs, f"Ожидание ответа от {who}"))
        _emit(title, body)

    # Каждый блок строго по одному статусу. Пустые блоки не выводятся.
    _simple_block("Подтвердили за собой", confirmed_by_eng, "Подтверждено")
    _accepted_block("Переданы (приняты)", accepted_by_pair)
    _pending_transfer_block("Переданы (ожидают ответа)", transfer_by_pair)
    _simple_block("Передача отклонена — ждёт решения инициатора",
                  rejected_by_eng, "Замена отказалась")
    _simple_block("Отказались", declined_by_eng, "Отказ")
    _simple_block("Ожидают первого ответа", waiting_by_eng, "Ожидает ответа")
    _simple_block("Пропущены", skipped_by_eng, "Не зарегистрирован")

    lines.append("")
    lines.append(SEP)
    from datetime import datetime as _dtnow
    lines.append(f"<i>Обновлено: {_dtnow.now().strftime('%d.%m.%Y %H:%M')}</i>")
    if resettable:
        lines.append("<i>🔄 — кнопки ниже сбрасывают ответ дежурного и переотправляют опрос</i>")

    return lines, resettable


async def _render_admin_current_poll(callback: CallbackQuery):
    session = await database.get_active_session()
    if not session:
        try:
            await callback.message.edit_text(
                "<b>Текущий опрос</b>\n"
                "\n"
                "<i>Сейчас активных опросов нет. Запустите новый через кнопку «Запустить дежурство».</i>",
                reply_markup=keyboards.back_keyboard(),
            )
        except Exception:
            pass
        await callback.answer()
        return

    aps = await database.get_session_assignment_projects(session["id"])
    legacy = await database.get_session_assignments(session["id"])
    assignment_by_eng = {a["engineer_id"]: a["id"] for a in legacy}
    all_eng = await database.get_all_engineers()
    by_id = {e["id"]: e for e in all_eng}

    # Resolve the latest transfer candidate for every pending transfer (async).
    latest_cand_map: dict[int, Optional[dict]] = {}
    for ap in aps:
        if ap["status"] == database.AP_TRANSFER_PENDING:
            latest_cand_map[ap["id"]] = await _latest_transfer_candidate(
                session["id"], ap["id"]
            )

    lines, resettable = _build_admin_poll_text(
        aps, by_id, session["period"], latest_cand_map, assignment_by_eng
    )

    try:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=keyboards.admin_current_poll_keyboard(session["id"], resettable),
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("poll_reset:"))
async def on_poll_reset(callback: CallbackQuery):
    """One-tap reset of a single participant straight from the admin summary."""
    if not _check_admin(callback.from_user.id, action="poll_reset"):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    assignment_id = int(callback.data.split(":")[1])
    a = await database.get_assignment(assignment_id)
    if not a:
        await callback.answer("Задание не найдено.", show_alert=True)
        return
    result = await _reset_assignment_and_resend(assignment_id)
    eng = result["engineer"]
    name = eng["full_name"] if eng else "?"
    if result["delivered"]:
        await callback.answer(f"{name}: статус сброшен, опрос переотправлен")
    else:
        await callback.answer(f"{name}: статус сброшен (сообщение не доставлено)", show_alert=True)
    # Re-render the summary so the person moves back to «Ожидают ответа»
    await _render_admin_current_poll(callback)
