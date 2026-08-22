import os
import json
from datetime import datetime
import aiosqlite
from config import DATABASE_PATH, TZ_TURKEY
from signal_evaluator import infer_dominant_side, evaluate_signal_result

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

        CREATE TABLE IF NOT EXISTS anomaly_match_actions (
            match_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'new',
            updated_at TEXT DEFAULT (datetime('now'))
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

    """)
    await db.commit()
    # Gemini analysis was removed; discard its obsolete local cache table.
    await db.execute("DROP TABLE IF EXISTS upcoming_analyses")
    await db.commit()
    # Preserve the latest existing row state as the match-wide state when
    # upgrading databases created before match actions were introduced.
    await db.execute(
        """INSERT OR IGNORE INTO anomaly_match_actions (match_id, status)
           SELECT a.match_id, a.status
           FROM anomalies AS a
           JOIN (
               SELECT match_id, MAX(id) AS latest_id
               FROM anomalies
               GROUP BY match_id
           ) AS latest ON latest.latest_id = a.id"""
    )
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
    # Migration: immutable signal prediction and completed-match grading.
    result_columns = (
        "ALTER TABLE anomalies ADD COLUMN dominant_side TEXT DEFAULT 'unknown'",
        "ALTER TABLE anomalies ADD COLUMN final_score_home INTEGER DEFAULT NULL",
        "ALTER TABLE anomalies ADD COLUMN final_score_away INTEGER DEFAULT NULL",
        "ALTER TABLE anomalies ADD COLUMN result_status TEXT DEFAULT 'pending'",
        "ALTER TABLE anomalies ADD COLUMN finished_at TEXT DEFAULT NULL",
        "ALTER TABLE anomalies ADD COLUMN deletion_reason TEXT DEFAULT NULL",
    )
    for column_sql in result_columns:
        try:
            await db.execute(column_sql)
        except Exception:
            pass
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_anomalies_result_status "
        "ON anomalies(result_status, deleted_at)"
    )
    await db.commit()
    await _backfill_dominant_sides(db)
    # Removed live-list/live-detection feature: discard its obsolete state.
    await db.execute("DROP TABLE IF EXISTS live_match_actions")
    await db.commit()


# ---- Anomaly CRUD ----


async def _backfill_dominant_sides(db: aiosqlite.Connection):
    """Populate prediction sides for records created before this feature."""
    cursor = await db.execute(
        "SELECT id, condition_type, score_home, score_away, stats_snapshot "
        "FROM anomalies WHERE COALESCE(dominant_side, 'unknown') = 'unknown'"
    )
    rows = await cursor.fetchall()
    updates = []
    for row in rows:
        try:
            stats = json.loads(row["stats_snapshot"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            stats = {}
        side = infer_dominant_side(
            row["condition_type"], row["score_home"], row["score_away"], stats
        )
        if side != "unknown":
            updates.append((side, row["id"]))
    if updates:
        await db.executemany(
            "UPDATE anomalies SET dominant_side = ? WHERE id = ?", updates
        )
        await db.commit()

async def insert_anomaly(
    match_id: str, home_team: str, away_team: str,
    score_home: int, score_away: int, minute: int,
    league: str, condition_type: str,
    triggered_rules: list[str], stats_snapshot: dict,
) -> tuple[int | None, bool, int]:
    """Insert or update anomaly. Returns (row_id, is_new, alert_number)."""
    db = await get_db()
    try:
        dominant_side = infer_dominant_side(
            condition_type, score_home, score_away, stats_snapshot
        )
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
            "SELECT status FROM anomaly_match_actions WHERE match_id = ?",
            (match_id,),
        )
        saved_action = await cursor.fetchone()
        status = saved_action["status"] if saved_action else "new"

        cursor = await db.execute(
            """INSERT INTO anomalies
               (match_id, home_team, away_team, score_home, score_away,
                minute, league, condition_type, triggered_rules, stats_snapshot,
                alert_number, detected_at_tr, dominant_side, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                match_id, home_team, away_team, score_home, score_away,
                minute, league, condition_type,
                json.dumps(triggered_rules), json.dumps(stats_snapshot),
                alert_number, turkey_now_str(), dominant_side, status,
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


async def get_deleted_anomalies(
    result_filter: str | None = None,
    hide_unique: bool = False,
    limit: int = 500,
):
    db = await get_db()
    clauses = ["deleted_at IS NOT NULL"]
    params: list = []
    if result_filter:
        clauses.append("result_status = ?")
        params.append(result_filter)
    if hide_unique:
        clauses.append("(match_signal_count > 1 OR match_max_alert_number > 1)")
    where = " AND ".join(clauses)
    cursor = await db.execute(
        f"""WITH counted AS (
                SELECT a.*,
                       COUNT(*) OVER (PARTITION BY match_id) AS match_signal_count,
                       MAX(COALESCE(alert_number, 1)) OVER (PARTITION BY match_id)
                           AS match_max_alert_number
                FROM anomalies AS a
            )
            SELECT * FROM counted
            WHERE {where}
            ORDER BY deleted_at DESC LIMIT ?""",
        params + [limit],
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_deleted_anomaly_summary(
    result_filter: str | None = None,
    hide_unique: bool = False,
) -> dict:
    db = await get_db()
    clauses = ["deleted_at IS NOT NULL"]
    params: list = []
    if result_filter:
        clauses.append("result_status = ?")
        params.append(result_filter)
    if hide_unique:
        clauses.append("(match_signal_count > 1 OR match_max_alert_number > 1)")
    where = " AND ".join(clauses)
    cursor = await db.execute(
        f"""WITH counted AS (
                SELECT a.*,
                       COUNT(*) OVER (PARTITION BY match_id) AS match_signal_count,
                       MAX(COALESCE(alert_number, 1)) OVER (PARTITION BY match_id)
                           AS match_max_alert_number
                FROM anomalies AS a
            ), filtered AS (
                SELECT * FROM counted WHERE {where}
            )
            SELECT
               COUNT(*) AS total,
               SUM(CASE WHEN result_status = 'successful' THEN 1 ELSE 0 END) AS successful,
               SUM(CASE WHEN result_status = 'failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN result_status = 'pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN result_status = 'unresolved' THEN 1 ELSE 0 END) AS unresolved,
               COUNT(DISTINCT CASE WHEN final_score_home IS NOT NULL
                                   AND final_score_away IS NOT NULL
                                   THEN match_id END) AS finished_matches
           FROM filtered""",
        params,
    )
    row = dict(await cursor.fetchone())
    successful = int(row.get("successful") or 0)
    failed = int(row.get("failed") or 0)
    evaluated = successful + failed
    return {
        "total": int(row.get("total") or 0),
        "successful": successful,
        "failed": failed,
        "pending": int(row.get("pending") or 0),
        "unresolved": int(row.get("unresolved") or 0),
        "evaluated": evaluated,
        "finished_matches": int(row.get("finished_matches") or 0),
        "success_rate": round(successful * 100 / evaluated, 1) if evaluated else 0.0,
    }


async def get_pending_anomaly_match_ids() -> list[str]:
    """Return matches that still need a final-result check, including trash."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT DISTINCT match_id FROM anomalies "
        "WHERE COALESCE(result_status, 'pending') = 'pending' "
        "AND final_score_home IS NULL AND final_score_away IS NULL"
    )
    return [str(row["match_id"]) for row in await cursor.fetchall()]


async def finalize_match_anomalies(
    match_id: str, final_score_home: int, final_score_away: int
) -> int:
    """Grade every pending signal for a finished match and archive active rows."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, dominant_side FROM anomalies "
        "WHERE match_id = ? AND COALESCE(result_status, 'pending') = 'pending'",
        (match_id,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return 0

    now_tr = turkey_now_str()
    for row in rows:
        result_status = evaluate_signal_result(
            row["dominant_side"], final_score_home, final_score_away
        )
        await db.execute(
            """UPDATE anomalies SET
                   final_score_home = ?, final_score_away = ?,
                   result_status = ?, finished_at = ?,
                   deletion_reason = CASE
                       WHEN deleted_at IS NULL THEN 'match_finished'
                       ELSE COALESCE(deletion_reason, 'manual')
                   END,
                   deleted_at = COALESCE(deleted_at, datetime('now'))
               WHERE id = ?""",
            (
                final_score_home, final_score_away, result_status,
                now_tr, row["id"],
            ),
        )
    await db.commit()
    return len(rows)


async def _set_match_statuses(db: aiosqlite.Connection, match_ids: list[str], status: str) -> int:
    """Persist and apply a state to every active signal of the given matches."""
    if not match_ids:
        return 0
    unique_match_ids = list(dict.fromkeys(match_ids))
    await db.executemany(
        """INSERT INTO anomaly_match_actions (match_id, status, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(match_id) DO UPDATE SET
             status=excluded.status,
             updated_at=excluded.updated_at""",
        [(match_id, status) for match_id in unique_match_ids],
    )
    placeholders = ",".join("?" for _ in unique_match_ids)
    cursor = await db.execute(
        f"UPDATE anomalies SET status = ? "
        f"WHERE match_id IN ({placeholders}) AND deleted_at IS NULL",
        [status] + unique_match_ids,
    )
    return cursor.rowcount


async def update_anomaly_status(anomaly_id: int, status: str) -> int:
    db = await get_db()
    cursor = await db.execute(
        "SELECT match_id FROM anomalies WHERE id = ? AND deleted_at IS NULL",
        (anomaly_id,),
    )
    row = await cursor.fetchone()
    updated = await _set_match_statuses(db, [row["match_id"]], status) if row else 0
    await db.commit()
    return updated


async def bulk_update_anomaly_status(ids: list[int], status: str) -> int:
    if not ids:
        return 0
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"SELECT DISTINCT match_id FROM anomalies "
        f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        ids,
    )
    match_ids = [str(row["match_id"]) for row in await cursor.fetchall()]
    updated = await _set_match_statuses(db, match_ids, status)
    await db.commit()
    return updated


async def soft_delete_anomalies(ids: list[int]):
    """Move anomalies to the trash by setting deleted_at."""
    if not ids:
        return
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"UPDATE anomalies SET deleted_at = datetime('now'), deletion_reason = 'manual' "
        f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        ids,
    )
    await db.commit()


async def soft_delete_all_anomalies():
    """Move all non-deleted anomalies to the trash."""
    db = await get_db()
    await db.execute(
        "UPDATE anomalies SET deleted_at = datetime('now'), deletion_reason = 'manual' "
        "WHERE deleted_at IS NULL"
    )
    await db.commit()


async def restore_anomalies(ids: list[int]):
    if not ids:
        return
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"UPDATE anomalies SET deleted_at = NULL, deletion_reason = NULL "
        f"WHERE id IN ({placeholders}) AND result_status = 'pending' "
        f"AND final_score_home IS NULL AND final_score_away IS NULL",
        ids,
    )
    await db.commit()
    return cursor.rowcount


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


async def clear_database():
    db = await get_db()
    await db.executescript("""
        DELETE FROM anomalies;
        DELETE FROM anomaly_match_actions;
        DELETE FROM upcoming_matches;
    """)
    await db.commit()


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


# ---- Upcoming Matches CRUD ----

async def upsert_upcoming_matches(matches: list[dict], scan_date: str) -> int:
    """Insert or update the rolling 24-hour fixture snapshot."""
    db = await get_db()
    count = 0
    for match in matches:
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
                match["event_id"], match["home_team"], match["away_team"],
                match["league"], match["start_time"], match["round_info"],
                scan_date,
            ),
        )
        if cursor.rowcount > 0:
            count += 1
    await db.commit()
    return count


async def get_upcoming_matches_db(
    scan_date: str | None = None,
    status_filter: str | None = None,
    min_start_time: int | None = None,
    max_start_time: int | None = None,
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
    if min_start_time is not None:
        clauses.append("CAST(start_time AS INTEGER) >= ?")
        params.append(min_start_time)
    if max_start_time is not None:
        clauses.append("CAST(start_time AS INTEGER) <= ?")
        params.append(max_start_time)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    cursor = await db.execute(
        f"SELECT * FROM upcoming_matches{where} ORDER BY start_time ASC LIMIT ?",
        params + [limit],
    )
    return [dict(row) for row in await cursor.fetchall()]


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
    await db.execute(f"DELETE FROM upcoming_matches WHERE id IN ({placeholders})", ids)
    await db.commit()


async def clear_upcoming_matches():
    db = await get_db()
    await db.execute("DELETE FROM upcoming_matches")
    await db.commit()


async def mark_upcoming_anomaly(event_ids: list[str], scan_date: str):
    """Mark rolling-snapshot fixtures for which a live anomaly was detected."""
    if not event_ids:
        return
    db = await get_db()
    placeholders = ",".join("?" for _ in event_ids)
    await db.execute(
        f"UPDATE upcoming_matches SET has_anomaly = 1 "
        f"WHERE event_id IN ({placeholders}) AND scan_date = ?",
        event_ids + [scan_date],
    )
    await db.commit()
