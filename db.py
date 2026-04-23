import os
import json
from datetime import datetime
import aiosqlite
from config import DATABASE_PATH, TZ_TURKEY

_db: aiosqlite.Connection | None = None


def turkey_now_str() -> str:
    return datetime.now(TZ_TURKEY).strftime("%Y-%m-%d %H:%M:%S")


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None or not _db.is_alive:
        os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
        _db = await aiosqlite.connect(DATABASE_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA synchronous=NORMAL")
    return _db


async def init_db():
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS anomalies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            score_home INTEGER DEFAULT 0,
            score_away INTEGER DEFAULT 0,
            minute INTEGER DEFAULT 0,
            league TEXT DEFAULT '',
            condition_type TEXT NOT NULL,
            triggered_rules TEXT NOT NULL,
            stats_snapshot TEXT,
            status TEXT DEFAULT 'new',
            notified INTEGER DEFAULT 0,
            alert_number INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_anomaly_match_score
            ON anomalies(match_id, condition_type, score_home, score_away);

        CREATE TABLE IF NOT EXISTS upcoming_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_text TEXT NOT NULL,
            match_count INTEGER DEFAULT 0,
            run_type TEXT DEFAULT 'morning',
            status TEXT DEFAULT 'new',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS upcoming_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            league TEXT DEFAULT '',
            start_time TEXT DEFAULT '',
            round_info TEXT DEFAULT '',
            status TEXT DEFAULT 'new',
            has_anomaly INTEGER DEFAULT 0,
            scan_date TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_upcoming_event
            ON upcoming_matches(event_id, scan_date);

        CREATE TABLE IF NOT EXISTS live_match_actions (
            event_id TEXT PRIMARY KEY,
            home_team TEXT DEFAULT '',
            away_team TEXT DEFAULT '',
            score_home INTEGER DEFAULT 0,
            score_away INTEGER DEFAULT 0,
            minute INTEGER DEFAULT 0,
            league TEXT DEFAULT '',
            status_desc TEXT DEFAULT '',
            status TEXT DEFAULT 'new',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    await db.commit()

    # Migration: add alert_number column & update index for existing databases
    try:
        await db.execute("ALTER TABLE anomalies ADD COLUMN alert_number INTEGER DEFAULT 1")
        await db.commit()
    except Exception:
        pass  # column already exists
    try:
        await db.execute("DROP INDEX IF EXISTS idx_anomaly_match")
        await db.commit()
    except Exception:
        pass
    # Migration: soft-delete column
    try:
        await db.execute("ALTER TABLE anomalies ADD COLUMN deleted_at TEXT DEFAULT NULL")
        await db.commit()
    except Exception:
        pass  # column already exists
    # Migration: persist anomaly detection time in Turkey local time.
    try:
        await db.execute("ALTER TABLE anomalies ADD COLUMN detected_at_tr TEXT DEFAULT ''")
        await db.commit()
    except Exception:
        pass  # column already exists
    try:
        await db.execute(
            "UPDATE anomalies "
            "SET detected_at_tr = datetime(created_at, '+3 hours') "
            "WHERE COALESCE(detected_at_tr, '') = '' "
            "AND COALESCE(created_at, '') != ''"
        )
        await db.commit()
    except Exception:
        pass
    # Migration: persist enough live-match metadata for the Canlı Tespit view.
    for column_sql in (
        "ALTER TABLE live_match_actions ADD COLUMN home_team TEXT DEFAULT ''",
        "ALTER TABLE live_match_actions ADD COLUMN away_team TEXT DEFAULT ''",
        "ALTER TABLE live_match_actions ADD COLUMN score_home INTEGER DEFAULT 0",
        "ALTER TABLE live_match_actions ADD COLUMN score_away INTEGER DEFAULT 0",
        "ALTER TABLE live_match_actions ADD COLUMN minute INTEGER DEFAULT 0",
        "ALTER TABLE live_match_actions ADD COLUMN league TEXT DEFAULT ''",
        "ALTER TABLE live_match_actions ADD COLUMN status_desc TEXT DEFAULT ''",
        "ALTER TABLE live_match_actions ADD COLUMN created_at TEXT DEFAULT ''",
    ):
        try:
            await db.execute(column_sql)
            await db.commit()
        except Exception:
            pass  # column already exists


# ---- Anomaly CRUD ----

async def insert_anomaly(
    match_id: str, home_team: str, away_team: str,
    score_home: int, score_away: int, minute: int,
    league: str, condition_type: str,
    triggered_rules: list[str], stats_snapshot: dict,
) -> tuple[int | None, bool, int]:
    """Insert or update anomaly. Returns (row_id, is_new, alert_number)."""
    db = await get_db()
    try:
        # Check if this exact match+condition+score already exists
        cursor = await db.execute(
            "SELECT id, alert_number FROM anomalies "
            "WHERE match_id = ? AND condition_type = ? AND score_home = ? AND score_away = ?",
            (match_id, condition_type, score_home, score_away),
        )
        existing = await cursor.fetchone()

        if existing:
            # Same score — just update stats in place
            await db.execute(
                """UPDATE anomalies SET minute=?,
                   triggered_rules=?, stats_snapshot=? WHERE id=?""",
                (
                    minute,
                    json.dumps(triggered_rules), json.dumps(stats_snapshot),
                    existing["id"],
                ),
            )
            await db.commit()
            return existing["id"], False, existing["alert_number"]

        # Count all existing alerts for this match (across all conditions & scores)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM anomalies WHERE match_id = ?",
            (match_id,),
        )
        count = (await cursor.fetchone())[0]
        alert_number = count + 1

        cursor = await db.execute(
            """INSERT INTO anomalies
               (match_id, home_team, away_team, score_home, score_away,
                minute, league, condition_type, triggered_rules, stats_snapshot,
                alert_number, detected_at_tr)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                match_id, home_team, away_team, score_home, score_away,
                minute, league, condition_type,
                json.dumps(triggered_rules), json.dumps(stats_snapshot),
                alert_number, turkey_now_str(),
            ),
        )
        await db.commit()
        return cursor.lastrowid, True, alert_number
    except Exception:
        return None, False, 0


async def get_anomalies(status_filter: str | None = None, limit: int = 200):
    db = await get_db()
    if status_filter:
        cursor = await db.execute(
            "SELECT * FROM anomalies WHERE status = ? AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (status_filter, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM anomalies WHERE deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_deleted_anomalies(limit: int = 500):
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM anomalies WHERE deleted_at IS NOT NULL "
        "ORDER BY deleted_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_anomaly_status(anomaly_id: int, status: str):
    db = await get_db()
    await db.execute(
        "UPDATE anomalies SET status = ? WHERE id = ? AND deleted_at IS NULL",
        (status, anomaly_id),
    )
    await db.commit()


async def bulk_update_anomaly_status(ids: list[int], status: str):
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"UPDATE anomalies SET status = ? "
        f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        [status] + ids,
    )
    await db.commit()


async def soft_delete_anomalies(ids: list[int]):
    """Move anomalies to the trash by setting deleted_at."""
    if not ids:
        return
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"UPDATE anomalies SET deleted_at = datetime('now') "
        f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        ids,
    )
    await db.commit()


async def soft_delete_all_anomalies():
    """Move all non-deleted anomalies to the trash."""
    db = await get_db()
    await db.execute(
        "UPDATE anomalies SET deleted_at = datetime('now') "
        "WHERE deleted_at IS NULL"
    )
    await db.commit()


async def restore_anomalies(ids: list[int]):
    if not ids:
        return
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"UPDATE anomalies SET deleted_at = NULL "
        f"WHERE id IN ({placeholders})",
        ids,
    )
    await db.commit()


async def delete_anomalies(ids: list[int]):
    """Permanently delete anomalies (used when purging trash items)."""
    if not ids:
        return
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"DELETE FROM anomalies WHERE id IN ({placeholders})", ids
    )
    await db.commit()


async def purge_deleted_anomalies():
    """Permanently delete every row currently in the trash."""
    db = await get_db()
    await db.execute("DELETE FROM anomalies WHERE deleted_at IS NOT NULL")
    await db.commit()


async def clear_anomalies():
    """Permanently delete every anomaly (active and trashed)."""
    db = await get_db()
    await db.execute("DELETE FROM anomalies")
    await db.commit()


async def mark_notified(anomaly_id: int):
    db = await get_db()
    await db.execute(
        "UPDATE anomalies SET notified = 1 WHERE id = ?", (anomaly_id,)
    )
    await db.commit()


# ---- Upcoming Analyses CRUD ----

async def insert_analysis(text: str, match_count: int, run_type: str) -> int:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO upcoming_analyses (analysis_text, match_count, run_type) VALUES (?, ?, ?)",
        (text, match_count, run_type),
    )
    await db.commit()
    return cursor.lastrowid


async def get_analyses(limit: int = 50):
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM upcoming_analyses ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_analyses(ids: list[int]):
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"DELETE FROM upcoming_analyses WHERE id IN ({placeholders})", ids
    )
    await db.commit()


async def clear_analyses():
    db = await get_db()
    await db.execute("DELETE FROM upcoming_analyses")
    await db.commit()


async def clear_database():
    db = await get_db()
    await db.executescript("""
        DELETE FROM anomalies;
        DELETE FROM upcoming_analyses;
        DELETE FROM upcoming_matches;
        DELETE FROM live_match_actions;
    """)
    await db.commit()


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


# ---- Upcoming Matches CRUD ----

async def upsert_upcoming_matches(matches: list[dict], scan_date: str) -> int:
    """Insert or update upcoming matches for a given scan date. Returns count inserted/updated."""
    db = await get_db()
    count = 0
    for m in matches:
        cursor = await db.execute(
            """INSERT INTO upcoming_matches
               (event_id, home_team, away_team, league, start_time, round_info, scan_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_id, scan_date) DO UPDATE SET
                 home_team=excluded.home_team,
                 away_team=excluded.away_team,
                 league=excluded.league,
                 start_time=excluded.start_time,
                 round_info=excluded.round_info""",
            (
                m["event_id"], m["home_team"], m["away_team"],
                m["league"], m["start_time"], m["round_info"], scan_date,
            ),
        )
        if cursor.rowcount > 0:
            count += 1
    await db.commit()
    return count


async def get_upcoming_matches_db(
    scan_date: str | None = None,
    status_filter: str | None = None,
    limit: int = 500,
):
    db = await get_db()
    clauses = []
    params: list = []
    if scan_date:
        clauses.append("scan_date = ?")
        params.append(scan_date)
    if status_filter:
        clauses.append("status = ?")
        params.append(status_filter)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    cursor = await db.execute(
        f"SELECT * FROM upcoming_matches{where} ORDER BY start_time ASC LIMIT ?",
        params + [limit],
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_upcoming_match_status(match_id: int, status: str):
    db = await get_db()
    await db.execute(
        "UPDATE upcoming_matches SET status = ? WHERE id = ?", (status, match_id)
    )
    await db.commit()


async def bulk_update_upcoming_status(ids: list[int], status: str):
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"UPDATE upcoming_matches SET status = ? WHERE id IN ({placeholders})",
        [status] + ids,
    )
    await db.commit()


async def delete_upcoming_matches(ids: list[int]):
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"DELETE FROM upcoming_matches WHERE id IN ({placeholders})", ids
    )
    await db.commit()


async def clear_upcoming_matches():
    db = await get_db()
    await db.execute("DELETE FROM upcoming_matches")
    await db.commit()


# ---- Live Match Actions CRUD ----

async def get_live_actions(event_ids: list[str] | None = None) -> dict[str, str]:
    """Return {event_id: status} map for the requested events (or all)."""
    db = await get_db()
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        cursor = await db.execute(
            f"SELECT event_id, status FROM live_match_actions "
            f"WHERE event_id IN ({placeholders})",
            event_ids,
        )
    else:
        cursor = await db.execute(
            "SELECT event_id, status FROM live_match_actions"
        )
    rows = await cursor.fetchall()
    return {r["event_id"]: r["status"] for r in rows}


def _live_meta_values(meta: dict | None) -> tuple:
    meta = meta or {}
    return (
        str(meta.get("home_team") or ""),
        str(meta.get("away_team") or ""),
        int(meta.get("score_home") or 0),
        int(meta.get("score_away") or 0),
        int(meta.get("minute") or 0),
        str(meta.get("league") or ""),
        str(meta.get("status_desc") or ""),
    )


async def set_live_action(event_id: str, status: str, meta: dict | None = None):
    db = await get_db()
    home_team, away_team, score_home, score_away, minute, league, status_desc = _live_meta_values(meta)
    await db.execute(
        """INSERT INTO live_match_actions
             (event_id, home_team, away_team, score_home, score_away, minute,
              league, status_desc, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
           ON CONFLICT(event_id) DO UPDATE SET
             home_team = COALESCE(NULLIF(excluded.home_team, ''), live_match_actions.home_team),
             away_team = COALESCE(NULLIF(excluded.away_team, ''), live_match_actions.away_team),
             score_home = CASE WHEN excluded.home_team != '' OR excluded.away_team != ''
                               THEN excluded.score_home ELSE live_match_actions.score_home END,
             score_away = CASE WHEN excluded.home_team != '' OR excluded.away_team != ''
                               THEN excluded.score_away ELSE live_match_actions.score_away END,
             minute = CASE WHEN excluded.home_team != '' OR excluded.away_team != ''
                           THEN excluded.minute ELSE live_match_actions.minute END,
             league = COALESCE(NULLIF(excluded.league, ''), live_match_actions.league),
             status_desc = COALESCE(NULLIF(excluded.status_desc, ''), live_match_actions.status_desc),
             status = excluded.status,
             updated_at = excluded.updated_at""",
        (
            event_id, home_team, away_team, score_home, score_away, minute,
            league, status_desc, status,
        ),
    )
    await db.commit()


async def bulk_set_live_actions(
    event_ids: list[str],
    status: str,
    metadata_by_id: dict[str, dict] | None = None,
):
    if not event_ids:
        return
    db = await get_db()
    for eid in event_ids:
        home_team, away_team, score_home, score_away, minute, league, status_desc = _live_meta_values(
            (metadata_by_id or {}).get(str(eid))
        )
        await db.execute(
            """INSERT INTO live_match_actions
                 (event_id, home_team, away_team, score_home, score_away, minute,
                  league, status_desc, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
               ON CONFLICT(event_id) DO UPDATE SET
                 home_team = COALESCE(NULLIF(excluded.home_team, ''), live_match_actions.home_team),
                 away_team = COALESCE(NULLIF(excluded.away_team, ''), live_match_actions.away_team),
                 score_home = CASE WHEN excluded.home_team != '' OR excluded.away_team != ''
                                   THEN excluded.score_home ELSE live_match_actions.score_home END,
                 score_away = CASE WHEN excluded.home_team != '' OR excluded.away_team != ''
                                   THEN excluded.score_away ELSE live_match_actions.score_away END,
                 minute = CASE WHEN excluded.home_team != '' OR excluded.away_team != ''
                               THEN excluded.minute ELSE live_match_actions.minute END,
                 league = COALESCE(NULLIF(excluded.league, ''), live_match_actions.league),
                 status_desc = COALESCE(NULLIF(excluded.status_desc, ''), live_match_actions.status_desc),
                 status = excluded.status,
                 updated_at = excluded.updated_at""",
            (
                eid, home_team, away_team, score_home, score_away, minute,
                league, status_desc, status,
            ),
        )
    await db.commit()


async def get_following_live_matches() -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT event_id, home_team, away_team, score_home, score_away,
                  minute, league, status_desc, status, created_at, updated_at
           FROM live_match_actions
           WHERE status = 'following'
           ORDER BY updated_at DESC"""
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def mark_upcoming_anomaly(event_ids: list[str], scan_date: str):
    """Mark upcoming matches that have a live anomaly detected."""
    if not event_ids:
        return
    db = await get_db()
    placeholders = ",".join("?" for _ in event_ids)
    await db.execute(
        f"UPDATE upcoming_matches SET has_anomaly = 1 WHERE event_id IN ({placeholders}) AND scan_date = ?",
        event_ids + [scan_date],
    )
    await db.commit()
