"""
High-level duty scheduling logic: sending notifications and building summaries.

Per-project replacement model: статус хранится на уровне пары
(инженер + проект) в таблице assignment_projects. Таблица duty_assignments
сохраняется как снимок (dual-write) для отката.
"""
import asyncio
import logging
import os
from typing import Optional
from aiogram import Bot
from app.db import database
from app import keyboards

MAX_REPLACEMENT_CHAIN = 3
SEP_SUMMARY = "——————"

logger = logging.getLogger(__name__)


def format_duty_text(period: str, projects: list[str]) -> str:
    """Build the standard duty notification text used at first send and on cancel."""
    from app.middlewares import security as _sec
    projects_block = "\n".join(f"· {_sec.html_safe(p)}" for p in projects)
    return (
        f"<b>Дежурство · <code>{_sec.html_safe(period)}</code></b>\n"
        "\n"
        "Проекты:\n"
        f"{projects_block}\n"
        "\n"
        f"{SEP_SUMMARY}\n"
        "<i>Выберите действие ниже</i>"
    )


async def send_duty_notifications(
    bot: Bot,
    session_id: int,
    period: str,
    duty_map: dict[str, list[str]],
    engineers_info: list[dict],
) -> dict:
    """
    Send duty confirmation requests to all engineers who have a Telegram user_id.
    Returns a dict:
      {
        "sent":    [engineer dicts who got the message successfully],
        "skipped": [(engineer dict, reason text), ...],
      }
    """
    sent: list[dict] = []
    skipped: list[tuple[dict, str]] = []

    from app.middlewares import security as _sec

    async def send_one(eng: dict, projects: list[str]):
        assignment_id = await database.create_assignment(session_id, eng["id"], projects)
        # New per-project model (dual-write alongside legacy duty_assignments)
        await database.create_assignment_projects(session_id, eng["id"], projects)
        target_user_id = eng["user_id"]
        logger.info(
            f"DUTY_SEND assignment_id={assignment_id} engineer_id={eng['id']} "
            f"name={eng['full_name']!r} tag={eng.get('telegram_tag')!r} "
            f"target_user_id={_sec.mask_user_id(target_user_id)} "
            f"projects={projects}"
        )
        text = format_duty_text(period, projects)
        try:
            sent_msg = await bot.send_message(
                chat_id=target_user_id,
                text=text,
                reply_markup=keyboards.duty_confirm_keyboard(assignment_id),
            )
            sent.append(eng)
            try:
                await database.record_sent_message(
                    assignment_id, eng["id"], target_user_id, sent_msg.message_id, "duty"
                )
            except Exception:
                logger.exception("record_sent_message failed in send_one")
        except Exception as e:
            logger.exception(
                f"DUTY_SEND_FAILED assignment_id={assignment_id} "
                f"target_user_id={_sec.mask_user_id(target_user_id)} error={type(e).__name__}"
            )
            await database.update_assignment_status(assignment_id, "unreachable")
            # Mark this engineer's project rows as unreachable too
            ap_rows = await database.get_projects_for_engineer(session_id, eng["id"])
            await database.bulk_set_project_status(
                [r["id"] for r in ap_rows], database.AP_NO_CONTACT
            )
            skipped.append((eng, "ошибка отправки"))

    async def _skip(eng: dict, projects: list[str], legacy_status: str, reason: str):
        """Record a participant who could not be reached, in both data models."""
        assignment_id = await database.create_assignment(session_id, eng["id"], projects)
        await database.update_assignment_status(assignment_id, legacy_status)
        ap_ids = await database.create_assignment_projects(session_id, eng["id"], projects)
        await database.bulk_set_project_status(ap_ids, database.AP_NO_CONTACT)
        skipped.append((eng, reason))

    tasks = []
    for eng in engineers_info:
        name = eng["full_name"]
        projects = duty_map.get(name, [])
        if not projects:
            continue
        if not eng.get("telegram_tag"):
            await _skip(eng, projects, "no_telegram", "нет Telegram")
            continue
        if not eng.get("user_id"):
            await _skip(eng, projects, "no_user_id", "не запустил бота")
            continue
        tasks.append(send_one(eng, projects))

    await asyncio.gather(*tasks)
    return {"sent": sent, "skipped": skipped}


async def check_all_answered(session_id: int) -> bool:
    """True when every project row in the session has a final status."""
    aps = await database.get_session_assignment_projects(session_id)
    if not aps:
        return False
    return all(ap["status"] in database.AP_RESOLVED_STATUSES for ap in aps)


# ─── Summary (per-project) ───────────────────────────────────────────────────

async def build_summary(session_id: int) -> str:
    """Per-project summary of a completed survey, grouped by outcome."""
    from app.middlewares import security as _sec
    aps = await database.get_session_assignment_projects(session_id)
    session = await database.get_duty_session(session_id)
    period = session["period"] if session else "?"

    all_eng = await database.get_all_engineers()
    by_id = {e["id"]: e for e in all_eng}

    def _who(eid):
        """'Имя Фамилия (@тег)' — обычный текст (НЕ <code>), @тег кликабелен."""
        e = by_id.get(eid)
        if not e:
            return "?"
        tag = (e.get("telegram_tag") or "").strip()
        if tag and tag not in ("-", "—", "–"):
            return _sec.html_safe(f"{e['full_name']} ({tag})")
        return _sec.html_safe(e["full_name"])

    # Каждая запись попадает РОВНО в один блок по своему статусу.
    # «Без дежурного» — это пометка справа, а не отдельный блок.
    NO_DUTY_MARK = "⚠️ без дежурного"
    confirmed: list[str] = []    # AP_CONFIRMED_SELF
    transferred: list[str] = []  # AP_TRANSFER_ACCEPTED
    declined: list[str] = []     # AP_DECLINED
    no_contact: list[str] = []   # AP_NO_CONTACT
    in_progress: list[str] = []  # pending / transfer_pending / transfer_rejected

    for ap in aps:
        proj = _sec.html_safe(ap["project_name"])
        orig = _who(ap["engineer_id"])
        st = ap["status"]
        if st == database.AP_CONFIRMED_SELF:
            confirmed.append(f"· {proj} · {orig}")
        elif st == database.AP_TRANSFER_ACCEPTED:
            handler = _who(ap["current_handler_id"])
            transferred.append(f"· {proj} · {orig} → {handler}")
        elif st == database.AP_DECLINED:
            declined.append(f"· {proj} · {orig} {NO_DUTY_MARK}")
        elif st == database.AP_NO_CONTACT:
            no_contact.append(f"· {proj} · {orig} {NO_DUTY_MARK}")
        else:  # pending / transfer_pending / transfer_rejected
            _wip = {
                database.AP_PENDING:          "ожидает ответа",
                database.AP_TRANSFER_PENDING: "передан — ждёт ответа замены",
                database.AP_TRANSFER_REJECTED: "замена отказалась — ждёт решения",
            }.get(st, st)
            in_progress.append(f"· {proj} · {orig} — {_wip}")

    total = len(aps)
    no_duty_count = len(declined) + len(no_contact)
    lines = [
        f"<b>Сводка опроса · <code>{_sec.html_safe(period)}</code></b>",
        "",
        f"Проектов: <b>{total}</b> · "
        f"за собой: <b>{len(confirmed)}</b> · "
        f"переданы: <b>{len(transferred)}</b> · "
        f"без дежурного: <b>{no_duty_count}</b>",
    ]

    # Строки блоков — в <pre> (моноширинно, копируется).
    def _section(title: str, items: list[str]):
        if not items:
            return
        lines.append("")
        lines.append(f"<b>{title}</b>")
        lines.append("<pre>" + "\n".join(items) + "</pre>")

    _section("Подтвердили за собой",            confirmed)
    _section("Переданы заменам",                transferred)
    _section("Отказались",                     declined)
    _section("Пропущены (не зарегистрированы)", no_contact)
    _section("В процессе",                     in_progress)

    lines.append("")
    lines.append(SEP_SUMMARY)
    lines.append("<i>Выберите действие ниже</i>")

    return "\n".join(lines)


# ─── xlsx data (per-project, grouped by current handler) ─────────────────────

async def build_schedule_data(session_id: int) -> list[dict]:
    """
    Rows for the final xlsx. Projects with a duty officer are grouped by the
    person who ultimately handles them (confirmed_self → original,
    transfer_accepted → the candidate). Projects without anyone become
    'БЕЗ ДЕЖУРНОГО' rows (one per project) so the gap is visible.
    """
    aps = await database.get_session_assignment_projects(session_id)
    by_handler: dict[int, list[str]] = {}
    orphans: list[tuple[str, str]] = []  # (project_name, original_name)

    for ap in aps:
        st = ap["status"]
        if st in (database.AP_CONFIRMED_SELF, database.AP_TRANSFER_ACCEPTED):
            hid = ap["current_handler_id"] or ap["engineer_id"]
            by_handler.setdefault(hid, []).append(ap["project_name"])
        else:
            orig = await database.get_engineer_by_id(ap["engineer_id"])
            orphans.append((ap["project_name"], orig["full_name"] if orig else "?"))

    rows: list[dict] = []
    for hid, projects in by_handler.items():
        eng = await database.get_engineer_by_id(hid)
        if not eng:
            continue
        rows.append({
            "full_name": eng["full_name"],
            "phone": eng.get("phone", ""),
            "telegram_tag": eng.get("telegram_tag", ""),
            "email": eng.get("email", ""),
            "projects": projects,
            "no_duty": False,
        })
    rows.sort(key=lambda r: (r["full_name"] or "").lower())

    for proj, orig_name in orphans:
        rows.append({
            "full_name": "БЕЗ ДЕЖУРНОГО",
            "phone": "",
            "telegram_tag": "",
            "email": "",
            "projects": [proj],
            "no_duty": True,
            "original_name": orig_name,
        })
    return rows


_AP_STATUS_TEXT = {
    database.AP_PENDING:           "Ожидает ответа",
    database.AP_CONFIRMED_SELF:    "Подтверждено за собой",
    database.AP_DECLINED:          "Отказ — без дежурного",
    database.AP_TRANSFER_PENDING:  "Передано — ожидает ответа замены",
    database.AP_TRANSFER_ACCEPTED: "Замена приняла",
    database.AP_TRANSFER_REJECTED: "Замена отказалась — ожидает решения",
    database.AP_NO_CONTACT:        "Нет связи",
}


async def build_current_state_data(session_id: int) -> list[dict]:
    """
    Snapshot of the session — one row per project, each carrying a
    human-readable status. Contacts shown are of the current handler.
    """
    aps = await database.get_session_assignment_projects(session_id)
    rows: list[dict] = []
    for ap in aps:
        orig = await database.get_engineer_by_id(ap["engineer_id"])
        st = ap["status"]
        if st in (database.AP_CONFIRMED_SELF, database.AP_TRANSFER_ACCEPTED):
            person = await database.get_engineer_by_id(ap["current_handler_id"]) or orig
        else:
            person = orig
        if not person:
            continue
        status_text = _AP_STATUS_TEXT.get(st, st)
        if st == database.AP_TRANSFER_ACCEPTED and orig and person and orig["id"] != person["id"]:
            status_text = f"Замена принята ({orig['full_name']} → {person['full_name']})"
        rows.append({
            "full_name": person["full_name"],
            "phone": person.get("phone", ""),
            "telegram_tag": person.get("telegram_tag", ""),
            "email": person.get("email", ""),
            "projects": [ap["project_name"]],
            "status": status_text,
            "no_duty": st in (database.AP_DECLINED, database.AP_NO_CONTACT),
        })
    return rows


# ─── Schedule image (PNG) with state-based caching ───────────────────────────
import hashlib
import tempfile

_IMG_CACHE_DIR = os.path.join(tempfile.gettempdir(), "duty_schedule_img")


async def _session_state_signature(session_id: int) -> str:
    """
    Stable hash of the session's per-project state. Changes whenever ANY
    project status or current handler changes — used to auto-invalidate the
    cached PNG (no manual invalidation hooks needed).

    Включает и КОНТАКТНЫЕ поля исполнителя (ФИО/телефон/тег/почта): картинка
    печатает их, но раньше в сигнатуру они не входили — после /import с
    переименованием тот же состав давал прежний хэш и старый кэш PNG.
    """
    aps = await database.get_session_assignment_projects(session_id)

    # Контакты исполнителей — тем же источником, что и рендер (get_engineer_by_id
    # содержит email). Кэшируем по id, чтобы не дёргать БД на каждый проект.
    handler_ids = {ap["current_handler_id"] or ap["engineer_id"] for ap in aps}
    contacts: dict[int, str] = {}
    for eid in handler_ids:
        e = await database.get_engineer_by_id(eid)
        contacts[eid] = "-" if not e else "~".join(
            str(e.get(k) or "") for k in
            ("full_name", "phone", "telegram_tag", "email")
        )

    parts = []
    for ap in sorted(aps, key=lambda a: a["id"]):
        handler = ap["current_handler_id"] or ap["engineer_id"]
        parts.append(
            f"{ap['id']}:{ap['status']}:{ap['current_handler_id']}:"
            f"{ap['project_name']}:{contacts.get(handler, '-')}"
        )
    raw = "|".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


async def build_schedule_image(session_id: int) -> Optional[str]:
    """
    Render (or reuse cached) PNG of the final schedule for a session.
    Returns the file path, or None if there are no rows to draw.

    Cache key = (session_id, state signature). When the state changes the
    signature changes → a fresh image is rendered; old files are pruned.
    """
    from app.services import image_render

    session = await database.get_duty_session(session_id)
    period = session["period"] if session else "?"
    rows = await build_schedule_data(session_id)
    if not rows:
        return None

    os.makedirs(_IMG_CACHE_DIR, exist_ok=True)
    sig = await _session_state_signature(session_id)
    path = os.path.join(_IMG_CACHE_DIR, f"schedule_{session_id}_{sig}.png")

    if os.path.exists(path):
        return path

    # Prune stale images for this session before rendering the new one
    try:
        prefix = f"schedule_{session_id}_"
        for fn in os.listdir(_IMG_CACHE_DIR):
            if fn.startswith(prefix) and fn != os.path.basename(path):
                try:
                    os.remove(os.path.join(_IMG_CACHE_DIR, fn))
                except OSError:
                    pass
    except OSError:
        pass

    # Rendering is CPU-bound — run it off the event loop
    await asyncio.to_thread(image_render.render_schedule_png, period, rows, path)
    return path

