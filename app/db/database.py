import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

import aiosqlite

from app.utils import normalize_search_text

DB_PATH = "duty_bot.db"

logger = logging.getLogger(__name__)

# Версия схемы для PRAGMA user_version: гейт одноразовых миграций данных.
DB_SCHEMA_VERSION = 1

# ── Общее соединение (одно на процесс) ───────────────────────────────────────
_conn: Optional[aiosqlite.Connection] = None
_conn_lock = asyncio.Lock()
# Один Lock на все ПИШУЩИЕ операции. У aiosqlite одно соединение обслуживает
# один рабочий поток, поэтому отдельные statement'ы сериализуются сами; Lock
# нужен, чтобы многошаговые последовательности (SELECT→INSERT/UPDATE,
# BEGIN…COMMIT) разных задач не перемешивались и не коммитили чужие половины
# транзакций. Читающие одиночные SELECT'ы в Lock не нуждаются.
_write_lock = asyncio.Lock()


async def _get_conn() -> aiosqlite.Connection:
    """Ленивая инициализация общего соединения с нужными PRAGMA."""
    global _conn
    if _conn is None:
        async with _conn_lock:
            if _conn is None:
                conn = await aiosqlite.connect(DB_PATH)
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA foreign_keys=ON")
                _conn = conn
    return _conn


async def close():
    """Закрыть общее соединение (вызывается при остановке бота)."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


@asynccontextmanager
async def _db():
    """Читающие операции: общее соединение, без write-lock (см. выше)."""
    yield await _get_conn()


@asynccontextmanager
async def _db_write():
    """Пишущие операции: общее соединение под _write_lock."""
    async with _write_lock:
        yield await _get_conn()


def _normalize_tag(tag):
    """Канонизировать Telegram-тег: '@MixedCase ' → '@mixedcase'.

    Пустые значения и прочерки ('', '-', '—', '–') возвращаются как есть —
    они означают «тега нет» и участвуют в этой семантике по всему коду.
    """
    if tag is None:
        return tag
    t = str(tag).strip()
    if t in ("", "-", "—", "–"):
        return t
    return "@" + t.lstrip("@").lower()


async def init_db():
    """Создать схему (idempotent), выполнить миграции, создать индексы."""
    async with _db_write() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS engineers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                full_name_normalized TEXT,
                phone TEXT,
                telegram_tag TEXT,
                email TEXT,
                user_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS duty_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finalized INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS duty_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                engineer_id INTEGER NOT NULL,
                projects TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                replacement_chain TEXT DEFAULT '[]',
                final_engineer_id INTEGER,
                FOREIGN KEY (session_id) REFERENCES duty_sessions(id),
                FOREIGN KEY (engineer_id) REFERENCES engineers(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id INTEGER NOT NULL,
                engineer_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (assignment_id) REFERENCES duty_assignments(id),
                FOREIGN KEY (engineer_id)   REFERENCES engineers(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                request_type TEXT NOT NULL,
                target_record_id INTEGER,
                proposed_tag TEXT,
                status TEXT DEFAULT 'pending',
                admin_comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_replacements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_engineer_id INTEGER NOT NULL,
                replacement_engineer_id INTEGER,
                period TEXT NOT NULL,
                reason TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (original_engineer_id) REFERENCES engineers(id),
                FOREIGN KEY (replacement_engineer_id) REFERENCES engineers(id)
            )
        """)
        # Per-project duty state (replaces the per-person duty_assignments model).
        # One row per (engineer, project) — each project carries its own status
        # and its own replacement chain counter.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS assignment_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                engineer_id INTEGER NOT NULL,
                project_name TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                current_handler_id INTEGER,
                replacement_chain_count INTEGER DEFAULT 0,
                FOREIGN KEY (session_id) REFERENCES duty_sessions(id),
                FOREIGN KEY (engineer_id) REFERENCES engineers(id)
            )
        """)
        # A transfer request groups several projects handed to ONE candidate.
        # The candidate accepts/declines the whole request at once.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transfer_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                initiator_engineer_id INTEGER NOT NULL,
                candidate_engineer_id INTEGER NOT NULL,
                project_ids TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES duty_sessions(id)
            )
        """)
        await db.commit()

        # Migration: older DBs lack engineers.full_name_normalized.
        # Наличие колонки проверяем явно через PRAGMA table_info; реальные
        # ошибки ALTER не глотаем — логируем и пробрасываем.
        async with db.execute("PRAGMA table_info(engineers)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "full_name_normalized" not in columns:
            try:
                await db.execute(
                    "ALTER TABLE engineers ADD COLUMN full_name_normalized TEXT"
                )
                await db.commit()
            except Exception:
                logger.exception("ALTER TABLE engineers ADD COLUMN failed")
                raise

        # Backfill normalized names for any rows that still miss them
        async with db.execute(
            "SELECT id, full_name FROM engineers "
            "WHERE full_name_normalized IS NULL OR full_name_normalized = ''"
        ) as cursor:
            rows = await cursor.fetchall()
        if rows:
            for eid, full_name in rows:
                await db.execute(
                    "UPDATE engineers SET full_name_normalized=? WHERE id=?",
                    (normalize_search_text(full_name), eid),
                )
            await db.commit()

        # Backfill: канонизировать telegram_tag ('@lowercase') в старых записях,
        # чтобы точное сравнение (link_user_id) и поиск (LOWER LIKE) совпадали.
        await db.execute(
            "UPDATE engineers "
            "SET telegram_tag = '@' || LOWER(LTRIM(TRIM(telegram_tag), '@')) "
            "WHERE telegram_tag IS NOT NULL "
            "  AND TRIM(telegram_tag) NOT IN ('', '-', '—', '–') "
            "  AND telegram_tag != '@' || LOWER(LTRIM(TRIM(telegram_tag), '@'))"
        )
        await db.commit()

        # UNIQUE-индексы там, где уникальность реальна (частичные):
        # один Telegram-аккаунт — одна запись; один тег — одна запись.
        # ВАЖНО: UNIQUE(full_name) НЕ создаём — тёзки легальны.
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_engineers_user_id "
            "ON engineers(user_id) WHERE user_id IS NOT NULL"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_engineers_telegram_tag "
            "ON engineers(telegram_tag) "
            "WHERE telegram_tag IS NOT NULL "
            "  AND TRIM(telegram_tag) NOT IN ('', '-', '—', '–')"
        )

        # Обычные индексы по горячим полям
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_ap_session ON assignment_projects(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_ap_engineer ON assignment_projects(engineer_id)",
            "CREATE INDEX IF NOT EXISTS idx_engineers_full_name ON engineers(full_name)",
            "CREATE INDEX IF NOT EXISTS idx_sent_messages_assignment ON sent_messages(assignment_id)",
            "CREATE INDEX IF NOT EXISTS idx_tr_session ON transfer_requests(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_tr_candidate ON transfer_requests(candidate_engineer_id)",
            "CREATE INDEX IF NOT EXISTS idx_pr_user_status ON pending_requests(user_id, status)",
        ):
            await db.execute(idx_sql)
        await db.commit()

        # One-shot миграция duty_assignments → assignment_projects.
        # Гейт — PRAGMA user_version: пустота assignment_projects больше не
        # критерий (delete_session может опустошить таблицу, и старый гейт
        # повторно развернул бы устаревшие duty_assignments).
        async with db.execute("PRAGMA user_version") as cursor:
            (schema_version,) = await cursor.fetchone()
        if schema_version < DB_SCHEMA_VERSION:
            await _migrate_duty_assignments_to_projects(db)
            await db.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
            await db.commit()


# ─── Per-project status constants (new replacement model) ────────────────────
AP_PENDING           = "pending"            # ждёт первого ответа дежурного
AP_CONFIRMED_SELF    = "confirmed_self"     # дежурный взял проект на себя
AP_DECLINED          = "declined"           # отказ, дежурного нет
AP_TRANSFER_PENDING  = "transfer_pending"   # передан замене, ждёт её ответа
AP_TRANSFER_ACCEPTED = "transfer_accepted"  # замена приняла проект
AP_TRANSFER_REJECTED = "transfer_rejected"  # замена отказалась (промежуточное)
AP_NO_CONTACT        = "no_contact"         # инженер недоступен (нет Telegram/бот/ошибка)

# Статусы, означающие что по проекту получен окончательный ответ.
AP_RESOLVED_STATUSES = (AP_CONFIRMED_SELF, AP_DECLINED, AP_TRANSFER_ACCEPTED, AP_NO_CONTACT)


def _map_old_assignment_status(status: str, chain: list, engineer_id: int,
                               final_engineer_id: Optional[int]) -> tuple[str, int, int]:
    """
    Translate a legacy duty_assignments row into (new_status, current_handler_id,
    replacement_chain_count) for the per-project model.
    """
    count = len(chain)
    last = chain[-1] if chain else None
    if status == "confirmed":
        if last and last.get("status") == "accepted":
            return AP_TRANSFER_ACCEPTED, (final_engineer_id or engineer_id), count
        return AP_CONFIRMED_SELF, engineer_id, count
    if status in ("declined", "chain_failed"):
        return AP_DECLINED, engineer_id, count
    if status == "pending":
        if last and last.get("status") == "pending":
            return AP_TRANSFER_PENDING, (last.get("engineer_id") or engineer_id), count
        # pending without chain, or chain bounced back to the initiator
        return AP_PENDING, engineer_id, count
    if status in ("no_telegram", "no_user_id", "unreachable"):
        return AP_NO_CONTACT, engineer_id, count
    return AP_PENDING, engineer_id, count


async def _migrate_duty_assignments_to_projects(db):
    """
    One-shot: for every legacy duty_assignments row create one assignment_projects
    row per project. Runs only while assignment_projects is still empty so it
    never double-migrates. The old duty_assignments table is left intact.
    """
    import json
    async with db.execute("SELECT COUNT(*) FROM assignment_projects") as cur:
        (already,) = await cur.fetchone()
    if already > 0:
        return
    async with db.execute(
        "SELECT id, session_id, engineer_id, projects, status, replacement_chain, "
        "final_engineer_id FROM duty_assignments"
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return
    migrated = 0
    for _aid, session_id, engineer_id, projects_json, status, chain_json, final_id in rows:
        try:
            projects = json.loads(projects_json) if projects_json else []
        except Exception:
            projects = []
        try:
            chain = json.loads(chain_json) if chain_json else []
        except Exception:
            chain = []
        new_status, handler, count = _map_old_assignment_status(
            status, chain, engineer_id, final_id
        )
        for proj in projects:
            await db.execute(
                "INSERT INTO assignment_projects "
                "(session_id, engineer_id, project_name, status, "
                " current_handler_id, replacement_chain_count) VALUES (?,?,?,?,?,?)",
                (session_id, engineer_id, proj, new_status, handler, count),
            )
            migrated += 1
    await db.commit()
    import logging
    logging.getLogger(__name__).info(
        f"MIGRATION duty_assignments→assignment_projects: {len(rows)} assignments "
        f"→ {migrated} project rows"
    )


# ─── assignment_projects CRUD (per-project replacement model) ────────────────
_AP_COLS = ("id, session_id, engineer_id, project_name, status, "
            "current_handler_id, replacement_chain_count")


def _ap_row(r) -> dict:
    return {
        "id": r[0], "session_id": r[1], "engineer_id": r[2],
        "project_name": r[3], "status": r[4],
        "current_handler_id": r[5], "replacement_chain_count": r[6],
    }


async def create_assignment_projects(session_id: int, engineer_id: int,
                                     projects: list[str]) -> list[int]:
    """Create one pending project row per project for an engineer. Returns ids."""
    ids: list[int] = []
    async with _db_write() as db:
        for proj in projects:
            cur = await db.execute(
                "INSERT INTO assignment_projects "
                "(session_id, engineer_id, project_name, status, current_handler_id) "
                "VALUES (?,?,?,?,?)",
                (session_id, engineer_id, proj, AP_PENDING, engineer_id),
            )
            ids.append(cur.lastrowid)
        await db.commit()
    return ids


async def get_assignment_project(ap_id: int) -> Optional[dict]:
    async with _db() as db:
        async with db.execute(
            f"SELECT {_AP_COLS} FROM assignment_projects WHERE id=?", (ap_id,)
        ) as cur:
            row = await cur.fetchone()
    return _ap_row(row) if row else None


async def get_projects_for_engineer(session_id: int, engineer_id: int) -> list[dict]:
    """All project rows where this engineer is the ORIGINAL duty officer."""
    async with _db() as db:
        async with db.execute(
            f"SELECT {_AP_COLS} FROM assignment_projects "
            "WHERE session_id=? AND engineer_id=? ORDER BY id",
            (session_id, engineer_id),
        ) as cur:
            rows = await cur.fetchall()
    return [_ap_row(r) for r in rows]


async def get_session_assignment_projects(session_id: int) -> list[dict]:
    async with _db() as db:
        async with db.execute(
            f"SELECT {_AP_COLS} FROM assignment_projects WHERE session_id=? ORDER BY id",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [_ap_row(r) for r in rows]


async def count_session_progress(session_id: int) -> tuple[int, int]:
    """
    Прогресс опроса по таблице assignment_projects (источник истины).
    Возвращает (answered, total) по УНИКАЛЬНЫМ инженерам:
      total   — DISTINCT engineer_id в сессии;
      answered — инженеры, у которых ВСЕ их проекты не в статусе pending
                 (человек считается ответившим только когда полностью
                 обработал все свои проекты).
    """
    rows = await get_session_assignment_projects(session_id)
    by_eng: dict[int, list[str]] = {}
    for ap in rows:
        by_eng.setdefault(ap["engineer_id"], []).append(ap["status"])
    total = len(by_eng)
    answered = sum(
        1 for statuses in by_eng.values()
        if all(s != AP_PENDING for s in statuses)
    )
    return answered, total


async def update_assignment_project(ap_id: int, *, status: Optional[str] = None,
                                    current_handler_id: Optional[int] = None,
                                    replacement_chain_count: Optional[int] = None):
    sets, params = [], []
    if status is not None:
        sets.append("status=?"); params.append(status)
    if current_handler_id is not None:
        sets.append("current_handler_id=?"); params.append(current_handler_id)
    if replacement_chain_count is not None:
        sets.append("replacement_chain_count=?"); params.append(replacement_chain_count)
    if not sets:
        return
    params.append(ap_id)
    async with _db_write() as db:
        await db.execute(
            f"UPDATE assignment_projects SET {', '.join(sets)} WHERE id=?", tuple(params)
        )
        await db.commit()


async def bulk_set_project_status(ap_ids: list[int], status: str,
                                  current_handler_id: Optional[int] = None):
    if not ap_ids:
        return
    async with _db_write() as db:
        for ap_id in ap_ids:
            if current_handler_id is not None:
                await db.execute(
                    "UPDATE assignment_projects SET status=?, current_handler_id=? WHERE id=?",
                    (status, current_handler_id, ap_id),
                )
            else:
                await db.execute(
                    "UPDATE assignment_projects SET status=? WHERE id=?",
                    (status, ap_id),
                )
        await db.commit()


async def reset_engineer_projects(session_id: int, engineer_id: int):
    """Reset all of an engineer's projects back to a fresh pending state."""
    async with _db_write() as db:
        await db.execute(
            "UPDATE assignment_projects "
            "SET status=?, current_handler_id=engineer_id, replacement_chain_count=0 "
            "WHERE session_id=? AND engineer_id=?",
            (AP_PENDING, session_id, engineer_id),
        )
        await db.commit()


# ─── transfer_requests CRUD ──────────────────────────────────────────────────
def _tr_row(r) -> dict:
    import json
    return {
        "id": r[0], "session_id": r[1], "initiator_engineer_id": r[2],
        "candidate_engineer_id": r[3], "project_ids": json.loads(r[4] or "[]"),
        "status": r[5], "created_at": r[6],
    }


_TR_COLS = ("id, session_id, initiator_engineer_id, candidate_engineer_id, "
            "project_ids, status, created_at")


async def create_transfer_request(session_id: int, initiator_id: int,
                                   candidate_id: int, project_ids: list[int]) -> int:
    import json
    async with _db_write() as db:
        cur = await db.execute(
            "INSERT INTO transfer_requests "
            "(session_id, initiator_engineer_id, candidate_engineer_id, project_ids) "
            "VALUES (?,?,?,?)",
            (session_id, initiator_id, candidate_id, json.dumps(project_ids)),
        )
        await db.commit()
        return cur.lastrowid


async def get_transfer_request(req_id: int) -> Optional[dict]:
    async with _db() as db:
        async with db.execute(
            f"SELECT {_TR_COLS} FROM transfer_requests WHERE id=?", (req_id,)
        ) as cur:
            row = await cur.fetchone()
    return _tr_row(row) if row else None


async def update_transfer_request_status(req_id: int, status: str):
    async with _db_write() as db:
        await db.execute(
            "UPDATE transfer_requests SET status=? WHERE id=?", (status, req_id)
        )
        await db.commit()


async def get_transfer_requests_for_candidate(
    session_id: int, candidate_id: int, status: Optional[str] = None
) -> list[dict]:
    """All transfer requests in a session addressed to a given candidate."""
    sql = (f"SELECT {_TR_COLS} FROM transfer_requests "
           "WHERE session_id=? AND candidate_engineer_id=?")
    params: list = [session_id, candidate_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id"
    async with _db() as db:
        async with db.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
    return [_tr_row(r) for r in rows]


async def get_pending_transfer_requests(session_id: int) -> list[dict]:
    """All still-pending transfer requests in a session (for reminders)."""
    async with _db() as db:
        async with db.execute(
            f"SELECT {_TR_COLS} FROM transfer_requests "
            "WHERE session_id=? AND status='pending' ORDER BY id",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [_tr_row(r) for r in rows]


async def cancel_pending_transfer_requests_for_initiator(session_id: int, initiator_id: int) -> int:
    """Mark all still-pending transfer requests started by an engineer as cancelled."""
    async with _db_write() as db:
        cur = await db.execute(
            "UPDATE transfer_requests SET status='cancelled' "
            "WHERE session_id=? AND initiator_engineer_id=? AND status='pending'",
            (session_id, initiator_id),
        )
        await db.commit()
        return cur.rowcount


async def get_declined_candidates_for_project(session_id: int, ap_id: int) -> set[int]:
    """
    Candidate engineer_ids who already declined a transfer that included this
    project — used for loop protection (can't re-offer the same project to them).
    Фильтр по session_id — в SQL: заявки чужих сессий не тянем.
    """
    import json
    async with _db() as db:
        async with db.execute(
            "SELECT candidate_engineer_id, project_ids FROM transfer_requests "
            "WHERE session_id=? AND status='declined'",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    result: set[int] = set()
    for cand_id, pids_json in rows:
        try:
            pids = json.loads(pids_json or "[]")
        except Exception:
            pids = []
        if ap_id in pids:
            result.add(cand_id)
    return result


# ─── pending_requests (account link / unlink approval) ──────────────────────
def _pending_request_row(r) -> dict:
    return {
        "id": r[0], "user_id": r[1], "request_type": r[2],
        "target_record_id": r[3], "proposed_tag": r[4], "status": r[5],
        "admin_comment": r[6], "created_at": r[7], "resolved_at": r[8],
    }


_PR_COLS = ("id, user_id, request_type, target_record_id, proposed_tag, "
            "status, admin_comment, created_at, resolved_at")


async def create_pending_request(
    user_id: int,
    request_type: str,            # 'link' | 'unlink'
    target_record_id: Optional[int],
    proposed_tag: Optional[str] = None,
) -> int:
    async with _db_write() as db:
        cursor = await db.execute(
            "INSERT INTO pending_requests "
            "(user_id, request_type, target_record_id, proposed_tag) VALUES (?,?,?,?)",
            (user_id, request_type, target_record_id, proposed_tag),
        )
        await db.commit()
        return cursor.lastrowid


async def get_pending_request_by_user(user_id: int) -> Optional[dict]:
    async with _db() as db:
        async with db.execute(
            f"SELECT {_PR_COLS} FROM pending_requests "
            "WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _pending_request_row(row) if row else None


async def get_pending_request(req_id: int) -> Optional[dict]:
    async with _db() as db:
        async with db.execute(
            f"SELECT {_PR_COLS} FROM pending_requests WHERE id=?", (req_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return _pending_request_row(row) if row else None


async def get_all_pending_requests() -> list[dict]:
    async with _db() as db:
        async with db.execute(
            f"SELECT {_PR_COLS} FROM pending_requests WHERE status='pending' ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [_pending_request_row(r) for r in rows]


async def count_pending_requests() -> int:
    async with _db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM pending_requests WHERE status='pending'"
        ) as cursor:
            (n,) = await cursor.fetchone()
    return n


async def resolve_pending_request(req_id: int, status: str, admin_comment: Optional[str] = None):
    async with _db_write() as db:
        await db.execute(
            "UPDATE pending_requests SET status=?, admin_comment=?, "
            "resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, admin_comment, req_id),
        )
        await db.commit()


async def get_last_resolved_request(user_id: int) -> Optional[dict]:
    async with _db() as db:
        async with db.execute(
            f"SELECT {_PR_COLS} FROM pending_requests "
            "WHERE user_id=? AND status IN ('approved','rejected') "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _pending_request_row(row) if row else None


# ─── pending_replacements ────────────────────────────────────────────────────
async def create_pending_replacement(
    original_engineer_id: int,
    replacement_engineer_id: Optional[int],
    period: str,
    reason: str,
    status: str = "pending",
) -> int:
    async with _db_write() as db:
        cursor = await db.execute(
            "INSERT INTO pending_replacements "
            "(original_engineer_id, replacement_engineer_id, period, reason, status) "
            "VALUES (?,?,?,?,?)",
            (original_engineer_id, replacement_engineer_id, period, reason, status),
        )
        await db.commit()
        return cursor.lastrowid


async def get_pending_replacement(rep_id: int) -> Optional[dict]:
    async with _db() as db:
        async with db.execute(
            "SELECT id, original_engineer_id, replacement_engineer_id, period, reason, status "
            "FROM pending_replacements WHERE id=?",
            (rep_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "original_engineer_id": row[1], "replacement_engineer_id": row[2],
        "period": row[3], "reason": row[4], "status": row[5],
    }


async def update_pending_replacement_status(rep_id: int, status: str):
    async with _db_write() as db:
        await db.execute(
            "UPDATE pending_replacements SET status=? WHERE id=?",
            (status, rep_id),
        )
        await db.commit()


async def get_active_pending_replacements_for_period(period: str) -> list[dict]:
    """Accepted, not yet applied — used by /duty to substitute names."""
    async with _db() as db:
        async with db.execute(
            "SELECT id, original_engineer_id, replacement_engineer_id, period, reason, status "
            "FROM pending_replacements "
            "WHERE period=? AND status='accepted' AND replacement_engineer_id IS NOT NULL",
            (period,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [{
        "id": r[0], "original_engineer_id": r[1], "replacement_engineer_id": r[2],
        "period": r[3], "reason": r[4], "status": r[5],
    } for r in rows]


async def mark_pending_replacement_applied(rep_id: int):
    await update_pending_replacement_status(rep_id, "applied")


async def upsert_engineer(full_name: str, phone: str, telegram_tag: str, email: str) -> str:
    """Обновить/создать инженера по ФИО.

    Возвращает 'inserted' | 'updated' | 'ambiguous'. 'ambiguous' — в базе
    больше одной записи с таким ФИО (тёзки): запись НЕ трогаем, молча
    обновлять «первого попавшегося» — тихая порча данных.
    Гонка check-then-insert закрыта _write_lock (внутри _db_write).
    """
    norm = normalize_search_text(full_name)
    telegram_tag = _normalize_tag(telegram_tag)
    async with _db_write() as db:
        async with db.execute(
            "SELECT id FROM engineers WHERE full_name = ?", (full_name,)
        ) as cursor:
            rows = await cursor.fetchall()
        if len(rows) > 1:
            logger.warning(
                f"UPSERT_AMBIGUOUS full_name={full_name!r}: {len(rows)} записей — пропуск"
            )
            return "ambiguous"
        if rows:
            await db.execute(
                "UPDATE engineers SET full_name_normalized=?, phone=?, telegram_tag=?, email=? WHERE id=?",
                (norm, phone, telegram_tag, email, rows[0][0]),
            )
            result = "updated"
        else:
            await db.execute(
                "INSERT INTO engineers (full_name, full_name_normalized, phone, telegram_tag, email) "
                "VALUES (?,?,?,?,?)",
                (full_name, norm, phone, telegram_tag, email),
            )
            result = "inserted"
        await db.commit()
        return result


async def bulk_upsert_engineers(rows: list[dict]) -> tuple[int, list[str]]:
    """
    Atomically upsert a list of engineer rows. Either all rows are persisted
    or none. Each dict must have keys: full_name, phone, telegram_tag, email.

    Returns (upserted_count, ambiguous_names): имена, по которым в базе
    оказалось БОЛЬШЕ одной записи (тёзки), пропускаются без изменений и
    возвращаются вызывающему для ручного уточнения администратором.
    Гонка check-then-insert закрыта _write_lock (внутри _db_write).
    """
    ambiguous: list[str] = []
    upserted = 0
    async with _db_write() as db:
        try:
            await db.execute("BEGIN")
            for r in rows:
                norm = normalize_search_text(r["full_name"])
                tag = _normalize_tag(r["telegram_tag"])
                async with db.execute(
                    "SELECT id FROM engineers WHERE full_name = ?",
                    (r["full_name"],),
                ) as cursor:
                    existing = await cursor.fetchall()
                if len(existing) > 1:
                    logger.warning(
                        f"UPSERT_AMBIGUOUS full_name={r['full_name']!r}: "
                        f"{len(existing)} записей — пропуск, требуется ручное уточнение"
                    )
                    ambiguous.append(r["full_name"])
                    continue
                if existing:
                    await db.execute(
                        "UPDATE engineers SET full_name_normalized=?, phone=?, "
                        "telegram_tag=?, email=? WHERE id=?",
                        (norm, r["phone"], tag, r["email"], existing[0][0]),
                    )
                else:
                    await db.execute(
                        "INSERT INTO engineers "
                        "(full_name, full_name_normalized, phone, telegram_tag, email) "
                        "VALUES (?,?,?,?,?)",
                        (r["full_name"], norm, r["phone"], tag, r["email"]),
                    )
                upserted += 1
            await db.commit()
            return upserted, ambiguous
        except Exception:
            await db.rollback()
            raise


async def link_user_id(telegram_tag: str, user_id: int) -> Optional[dict]:
    """
    Link a Telegram user_id to an engineer record found by telegram_tag.
    Enforces uniqueness: the same user_id cannot stay attached to other records.
    Тег сравнивается в каноническом виде ('@lowercase') — так же, как он
    хранится после нормализации на записи, и согласованно с поиском.
    """
    canonical = _normalize_tag(telegram_tag)
    async with _db_write() as db:
        async with db.execute(
            "SELECT id, full_name, phone, telegram_tag FROM engineers WHERE telegram_tag = ?",
            (canonical,),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            # Detach this user_id from any other record before attaching here.
            await db.execute(
                "UPDATE engineers SET user_id=NULL WHERE user_id=? AND id<>?",
                (user_id, row[0]),
            )
            await db.execute(
                "UPDATE engineers SET user_id=? WHERE id=?", (user_id, row[0])
            )
            await db.commit()
            return {"id": row[0], "full_name": row[1], "phone": row[2], "telegram_tag": row[3]}
    return None


async def unlink_user_id(engineer_id: int):
    async with _db_write() as db:
        await db.execute(
            "UPDATE engineers SET user_id=NULL WHERE id=?", (engineer_id,)
        )
        await db.commit()


async def reset_all_bindings_except(user_id_to_keep: int) -> int:
    """
    Clear user_id for every engineer record except those linked to the given user_id.
    Returns number of cleared rows.
    """
    async with _db_write() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM engineers WHERE user_id IS NOT NULL AND user_id<>?",
            (user_id_to_keep,),
        ) as cursor:
            (count,) = await cursor.fetchone()
        await db.execute(
            "UPDATE engineers SET user_id=NULL WHERE user_id IS NOT NULL AND user_id<>?",
            (user_id_to_keep,),
        )
        await db.commit()
        return count


async def search_linked_engineers(query: str) -> list[dict]:
    """Search engineers with a linked user_id only — same semantics as search_engineers."""
    return await _run_engineer_search(query, linked_only=True)


async def link_user_id_by_id(engineer_id: int, user_id: int):
    """Attach user_id to a specific engineer; detach it from any other record first."""
    async with _db_write() as db:
        await db.execute(
            "UPDATE engineers SET user_id=NULL WHERE user_id=? AND id<>?",
            (user_id, engineer_id),
        )
        await db.execute(
            "UPDATE engineers SET user_id=? WHERE id=?", (user_id, engineer_id)
        )
        await db.commit()


async def get_engineer_by_user_id(user_id: int) -> Optional[dict]:
    async with _db() as db:
        async with db.execute(
            "SELECT id, full_name, phone, telegram_tag FROM engineers WHERE user_id=?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        return {"id": row[0], "full_name": row[1], "phone": row[2], "telegram_tag": row[3]}
    return None


async def get_engineer_by_id(engineer_id: int) -> Optional[dict]:
    async with _db() as db:
        async with db.execute(
            "SELECT id, full_name, phone, telegram_tag, user_id, email FROM engineers WHERE id=?",
            (engineer_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        return {"id": row[0], "full_name": row[1], "phone": row[2], "telegram_tag": row[3], "user_id": row[4], "email": row[5]}
    return None


MAX_QUERY_WORDS = 5
MAX_QUERY_LENGTH = 100


class QueryTooLong(ValueError):
    """Raised when the search query exceeds length / word-count limits."""


def _normalized_words(q: str) -> list[str]:
    """
    Validate length / word count and return normalized search words
    (lowercased, ё→е). Raises QueryTooLong on limit violation.
    """
    if len(q) > MAX_QUERY_LENGTH:
        raise QueryTooLong("Слишком длинный запрос, введите имя и фамилию.")
    words = normalize_search_text(q).split()
    if len(words) > MAX_QUERY_WORDS:
        raise QueryTooLong("Слишком длинный запрос, введите имя и фамилию.")
    return words


async def get_all_engineers() -> list[dict]:
    """Bulk fetch — used when you need to look up many engineers at once."""
    async with _db() as db:
        async with db.execute(
            "SELECT id, full_name, phone, telegram_tag, user_id FROM engineers"
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"id": r[0], "full_name": r[1], "phone": r[2], "telegram_tag": r[3], "user_id": r[4]} for r in rows]


async def get_linked_engineers() -> list[dict]:
    """All engineers with a linked Telegram user_id (registered in the bot)."""
    async with _db() as db:
        async with db.execute(
            "SELECT id, full_name, phone, telegram_tag, user_id FROM engineers "
            "WHERE user_id IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"id": r[0], "full_name": r[1], "phone": r[2], "telegram_tag": r[3], "user_id": r[4]} for r in rows]


async def count_linked_engineers() -> int:
    async with _db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM engineers WHERE user_id IS NOT NULL"
        ) as cursor:
            (n,) = await cursor.fetchone()
    return n


def _engineer_search_row(r) -> dict:
    return {"id": r[0], "full_name": r[1], "phone": r[2], "telegram_tag": r[3], "user_id": r[4]}


async def _run_engineer_search(query: str, *, linked_only: bool) -> list[dict]:
    """
    Search engineers by Telegram tag (query starts with '@') or by name.
    Name search is case-insensitive, Е/Ё-insensitive and word-order-independent:
    every normalized word must appear as a substring of full_name_normalized.
      'Фёдорова'      → matches 'Федорова'
      'Семён Алёшин'  → matches 'Алешин Семен'
    Tag search is case-insensitive.
    """
    q = query.strip()
    if not q:
        return []

    base_cols = "SELECT id, full_name, phone, telegram_tag, user_id FROM engineers"
    where_extra = " AND user_id IS NOT NULL" if linked_only else ""

    if q.startswith("@"):
        if len(q) > MAX_QUERY_LENGTH:
            raise QueryTooLong("Слишком длинный запрос.")
        sql = f"{base_cols} WHERE LOWER(telegram_tag) LIKE ?{where_extra}"
        params: list = [f"%{q.lower()}%"]
    else:
        words = _normalized_words(q)
        if not words:
            return []
        clause = " AND ".join("full_name_normalized LIKE ?" for _ in words)
        sql = f"{base_cols} WHERE ({clause}){where_extra}"
        params = [f"%{w}%" for w in words]

    async with _db() as db:
        async with db.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
    return [_engineer_search_row(r) for r in rows]


async def search_engineers(query: str) -> list[dict]:
    return await _run_engineer_search(query, linked_only=False)


async def create_duty_session(period: str) -> int:
    """Создать сессию опроса, атомарно вытеснив ранее активные.

    Любые ещё активные (finalized=0) сессии помечаются SESSION_SUPERSEDED в
    той же транзакции — иначе они копятся как «активные» и всплывают в
    get_active_session после отмены свежей (баг «график отменённого опроса»).
    История сохраняется: строки не удаляются, только меняют статус.
    """
    async with _db_write() as db:
        cur = await db.execute(
            "UPDATE duty_sessions SET finalized=? WHERE finalized=?",
            (SESSION_SUPERSEDED, SESSION_ACTIVE),
        )
        superseded = cur.rowcount
        cursor = await db.execute(
            "INSERT INTO duty_sessions (period) VALUES (?)", (period,)
        )
        new_id = cursor.lastrowid
        await db.commit()
    if superseded:
        logger.info(
            f"SESSION_SUPERSEDED count={superseded} on new session={new_id} "
            f"period={period!r}"
        )
    return new_id


async def get_duty_session(session_id: int) -> Optional[dict]:
    async with _db() as db:
        async with db.execute(
            "SELECT id, period, finalized FROM duty_sessions WHERE id=?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        return {"id": row[0], "period": row[1], "finalized": row[2]}
    return None


async def create_assignment(session_id: int, engineer_id: int, projects: list[str]) -> int:
    import json
    async with _db_write() as db:
        cursor = await db.execute(
            "INSERT INTO duty_assignments (session_id, engineer_id, projects, final_engineer_id) VALUES (?,?,?,?)",
            (session_id, engineer_id, json.dumps(projects, ensure_ascii=False), engineer_id),
        )
        await db.commit()
        return cursor.lastrowid


async def get_assignment(assignment_id: int) -> Optional[dict]:
    import json
    async with _db() as db:
        async with db.execute(
            "SELECT id, session_id, engineer_id, projects, status, replacement_chain, final_engineer_id "
            "FROM duty_assignments WHERE id=?",
            (assignment_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        return {
            "id": row[0], "session_id": row[1], "engineer_id": row[2],
            "projects": json.loads(row[3]), "status": row[4],
            "replacement_chain": json.loads(row[5]), "final_engineer_id": row[6],
        }
    return None


async def update_assignment_status(assignment_id: int, status: str):
    async with _db_write() as db:
        await db.execute(
            "UPDATE duty_assignments SET status=? WHERE id=?", (status, assignment_id)
        )
        await db.commit()


async def update_assignment_replacement(assignment_id: int, chain: list, final_engineer_id: int):
    import json
    async with _db_write() as db:
        await db.execute(
            "UPDATE duty_assignments SET replacement_chain=?, final_engineer_id=? WHERE id=?",
            (json.dumps(chain, ensure_ascii=False), final_engineer_id, assignment_id),
        )
        await db.commit()


async def get_session_assignments(session_id: int) -> list[dict]:
    import json
    async with _db() as db:
        async with db.execute(
            "SELECT id, session_id, engineer_id, projects, status, replacement_chain, final_engineer_id "
            "FROM duty_assignments WHERE session_id=?",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "session_id": r[1], "engineer_id": r[2],
            "projects": json.loads(r[3]), "status": r[4],
            "replacement_chain": json.loads(r[5]), "final_engineer_id": r[6],
        }
        for r in rows
    ]


async def finalize_session(session_id: int):
    async with _db_write() as db:
        await db.execute(
            "UPDATE duty_sessions SET finalized=1 WHERE id=?", (session_id,)
        )
        await db.commit()


# Session status values stored in duty_sessions.finalized
SESSION_ACTIVE = 0
SESSION_FINALIZED = 1
SESSION_CANCELLED = 2
# «Вытеснена» новым опросом (запуск нового опроса без «Пересоздать»).
# Отдельный статус, а не переиспользование CANCELLED: это другое событие в
# истории (не ручная отмена админом), и is_session_cancelled его не считает
# отменой. Сессия перестаёт быть активной (finalized != 0).
SESSION_SUPERSEDED = 3


async def cancel_session(session_id: int):
    """Mark the session as cancelled — kept in DB for history but treated as inactive."""
    async with _db_write() as db:
        await db.execute(
            "UPDATE duty_sessions SET finalized=? WHERE id=?",
            (SESSION_CANCELLED, session_id),
        )
        await db.commit()


async def is_session_cancelled(session_id: int) -> bool:
    s = await get_duty_session(session_id)
    return bool(s and s["finalized"] == SESSION_CANCELLED)


# ─── sent_messages tracking ──────────────────────────────────────────────────
async def record_sent_message(
    assignment_id: int,
    engineer_id: int,
    chat_id: int,
    message_id: int,
    kind: str,  # 'duty' | 'replacement'
):
    async with _db_write() as db:
        await db.execute(
            "INSERT INTO sent_messages "
            "(assignment_id, engineer_id, chat_id, message_id, kind) "
            "VALUES (?,?,?,?,?)",
            (assignment_id, engineer_id, chat_id, message_id, kind),
        )
        await db.commit()


async def get_sent_messages_for(
    assignment_id: int,
    engineer_id: int,
    *,
    kind: Optional[str] = None,
) -> list[dict]:
    sql = (
        "SELECT id, assignment_id, engineer_id, chat_id, message_id, kind "
        "FROM sent_messages WHERE assignment_id=? AND engineer_id=?"
    )
    params: list = [assignment_id, engineer_id]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    async with _db() as db:
        async with db.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
    return [{
        "id": r[0], "assignment_id": r[1], "engineer_id": r[2],
        "chat_id": r[3], "message_id": r[4], "kind": r[5],
    } for r in rows]


async def delete_sent_messages_for(assignment_id: int, engineer_id: int, *, kind: Optional[str] = None):
    sql = "DELETE FROM sent_messages WHERE assignment_id=? AND engineer_id=?"
    params: list = [assignment_id, engineer_id]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    async with _db_write() as db:
        await db.execute(sql, tuple(params))
        await db.commit()


async def get_all_sent_messages_for_assignment(assignment_id: int) -> list[dict]:
    async with _db() as db:
        async with db.execute(
            "SELECT id, assignment_id, engineer_id, chat_id, message_id, kind "
            "FROM sent_messages WHERE assignment_id=?",
            (assignment_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [{
        "id": r[0], "assignment_id": r[1], "engineer_id": r[2],
        "chat_id": r[3], "message_id": r[4], "kind": r[5],
    } for r in rows]


async def delete_all_sent_messages_for_assignment(assignment_id: int):
    async with _db_write() as db:
        await db.execute("DELETE FROM sent_messages WHERE assignment_id=?", (assignment_id,))
        await db.commit()


async def reset_assignment(assignment_id: int):
    """
    Reset an assignment to a fresh 'pending' state:
      status='pending', replacement_chain=[], final_engineer_id=engineer_id.
    Used when an admin re-sends the poll to a person who already answered.
    """
    async with _db_write() as db:
        async with db.execute(
            "SELECT engineer_id FROM duty_assignments WHERE id=?", (assignment_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return
        await db.execute(
            "UPDATE duty_assignments "
            "SET status='pending', replacement_chain='[]', final_engineer_id=? "
            "WHERE id=?",
            (row[0], assignment_id),
        )
        await db.commit()


async def get_active_assignments_for_engineer(engineer_id: int) -> list[dict]:
    """
    Return assignments in any non-finalized session that involve this engineer —
    either as the original duty officer (engineer_id), the current responsible
    person (final_engineer_id), or anywhere in the replacement chain.
    """
    import json
    async with _db() as db:
        async with db.execute(
            "SELECT a.id, a.session_id, a.engineer_id, a.projects, a.status, "
            "       a.replacement_chain, a.final_engineer_id "
            "FROM duty_assignments a JOIN duty_sessions s ON s.id = a.session_id "
            "WHERE s.finalized = 0 "
            "ORDER BY a.session_id DESC, a.id DESC",
            (),
        ) as cursor:
            rows = await cursor.fetchall()
    result = []
    for r in rows:
        chain = json.loads(r[5])
        chain_ids = {step.get("engineer_id") for step in chain}
        if engineer_id == r[2] or engineer_id == r[6] or engineer_id in chain_ids:
            result.append({
                "id": r[0], "session_id": r[1], "engineer_id": r[2],
                "projects": json.loads(r[3]), "status": r[4],
                "replacement_chain": chain, "final_engineer_id": r[6],
            })
    return result


async def get_active_session() -> Optional[dict]:
    """Return the most recent active (finalized=0) session, or None.

    Если активных сессий больше одной — это аномалия (create_duty_session
    должен был вытеснить прежние): логируем WARNING со списком id, но
    поведение сохраняем — берём свежайшую. Молчать нельзя: именно тишина
    прятала баг «график отменённого опроса».
    """
    async with _db() as db:
        async with db.execute(
            "SELECT id, period, finalized FROM duty_sessions "
            "WHERE finalized=0 ORDER BY id DESC"
        ) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            "MULTIPLE_ACTIVE_SESSIONS: %d активных (finalized=0), ids=%s; "
            "беру свежайшую id=%s",
            len(rows), [r[0] for r in rows], rows[0][0],
        )
    row = rows[0]
    return {"id": row[0], "period": row[1], "finalized": row[2]}


async def delete_session(session_id: int):
    """Delete a session and all its assignments (legacy + per-project model).

    sent_messages удаляются первыми: у них FK на duty_assignments, и с
    PRAGMA foreign_keys=ON удаление assignments с живыми детьми упало бы.
    """
    async with _db_write() as db:
        await db.execute(
            "DELETE FROM sent_messages WHERE assignment_id IN "
            "(SELECT id FROM duty_assignments WHERE session_id=?)",
            (session_id,),
        )
        await db.execute("DELETE FROM duty_assignments WHERE session_id=?", (session_id,))
        await db.execute("DELETE FROM assignment_projects WHERE session_id=?", (session_id,))
        await db.execute("DELETE FROM transfer_requests WHERE session_id=?", (session_id,))
        await db.execute("DELETE FROM duty_sessions WHERE id=?", (session_id,))
        await db.commit()


async def get_assignment_by_session_and_engineer(session_id: int, engineer_id: int) -> Optional[dict]:
    import json
    async with _db() as db:
        async with db.execute(
            "SELECT id, session_id, engineer_id, projects, status, replacement_chain, final_engineer_id "
            "FROM duty_assignments WHERE session_id=? AND final_engineer_id=? AND status='pending'",
            (session_id, engineer_id),
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        return {
            "id": row[0], "session_id": row[1], "engineer_id": row[2],
            "projects": json.loads(row[3]), "status": row[4],
            "replacement_chain": json.loads(row[5]), "final_engineer_id": row[6],
        }
    return None
