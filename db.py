import os
import json
import aiosqlite
from config import DATABASE_PATH

_db: aiosqlite.Connection | None = None


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
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_anomaly_match
            ON anomalies(match_id, condition_type);

        CREATE TABLE IF NOT EXISTS upcoming_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_text TEXT NOT NULL,
            match_count INTEGER DEFAULT 0,
            run_type TEXT DEFAULT 'morning',
            status TEXT DEFAULT 'new',
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    await db.commit()


# ---- Anomaly CRUD ----

async def insert_anomaly(
    match_id: str, home_team: str, away_team: str,
    score_home: int, score_away: int, minute: int,
    league: str, condition_type: str,
    triggered_rules: list[str], stats_snapshot: dict,
) -> int | None:
    """Insert anomaly. Returns row id or None if duplicate."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO anomalies
               (match_id, home_team, away_team, score_home, score_away,
                minute, league, condition_type, triggered_rules, stats_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                match_id, home_team, away_team, score_home, score_away,
                minute, league, condition_type,
                json.dumps(triggered_rules), json.dumps(stats_snapshot),
            ),
        )
        await db.commit()
        return cursor.lastrowid if cursor.rowcount > 0 else None
    except Exception:
        return None


async def get_anomalies(status_filter: str | None = None, limit: int = 200):
    db = await get_db()
    if status_filter:
        cursor = await db.execute(
            "SELECT * FROM anomalies WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status_filter, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM anomalies ORDER BY created_at DESC LIMIT ?", (limit,)
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_anomaly_status(anomaly_id: int, status: str):
    db = await get_db()
    await db.execute(
        "UPDATE anomalies SET status = ? WHERE id = ?", (status, anomaly_id)
    )
    await db.commit()


async def bulk_update_anomaly_status(ids: list[int], status: str):
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"UPDATE anomalies SET status = ? WHERE id IN ({placeholders})",
        [status] + ids,
    )
    await db.commit()


async def delete_anomalies(ids: list[int]):
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"DELETE FROM anomalies WHERE id IN ({placeholders})", ids
    )
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


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None
