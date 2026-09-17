import os
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
import json
from datetime import datetime
import aiosqlite
from config import DATABASE_PATH, TZ_TURKEY
from signal_evaluator import (
    infer_dominant_side,
    evaluate_signal_result,
    evaluate_selected_team_outcome,
    evaluate_equalization,
)

logger = logging.getLogger(__name__)
_connection_slots = asyncio.Semaphore(4)
_write_lock = asyncio.Lock()


def turkey_now_str() -> str:
    return datetime.now(TZ_TURKEY).strftime("%Y-%m-%d %H:%M:%S")


@asynccontextmanager
async def get_db(*, write: bool = False):
    """Own a bounded, short-lived connection and its transaction.

    Serialize local writers before opening connections; SQLite's busy timeout
    also handles other processes. WAL readers can continue during a write.
    """
    if write:
        await _write_lock.acquire()
    try:
        async with _connection_slots:
            os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
            async with aiosqlite.connect(DATABASE_PATH, timeout=15) as connection:
                connection.row_factory = aiosqlite.Row
                await connection.execute("PRAGMA busy_timeout=15000")
                await connection.execute("PRAGMA synchronous=NORMAL")
                if write:
                    await connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                finally:
                    # Roll back uncommitted work on errors, cancellation, or
                    # early returns; never commit another caller's transaction.
                    if connection.in_transaction:
                        await connection.rollback()
    finally:
        if write:
            _write_lock.release()


async def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as connection:
        await connection.execute("PRAGMA journal_mode=WAL")
    async with get_db(write=True) as db:
        await db.executescript("""
            DROP TABLE IF EXISTS live_match_actions;

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

        # Inspect the schema instead of hiding unrelated migration failures.
        cursor = await db.execute("PRAGMA table_info(anomalies)")
        columns = {row["name"] for row in await cursor.fetchall()}
        migrations = {
            "alert_number": "INTEGER DEFAULT 1",
            "deleted_at": "TEXT DEFAULT NULL",
            "detected_at_tr": "TEXT DEFAULT ''",
            "dominant_side": "TEXT DEFAULT 'unknown'",
            "final_score_home": "INTEGER DEFAULT NULL",
            "final_score_away": "INTEGER DEFAULT NULL",
            "result_status": "TEXT DEFAULT 'pending'",
            "finished_at": "TEXT DEFAULT NULL",
            "deletion_reason": "TEXT DEFAULT NULL",
            "result_checked_at": "TEXT DEFAULT NULL",
            # V2 first-signal evidence.  These columns are intentionally NULL
            # for legacy rows; migration cannot recreate an historical choice.
            "selected_side": "TEXT DEFAULT NULL",
            "triggered_groups": "TEXT DEFAULT NULL",
            "missing_fields": "TEXT DEFAULT NULL",
            "stats_period": "TEXT DEFAULT NULL",
            "event_fetched_at": "REAL DEFAULT NULL",
            "stats_fetched_at": "REAL DEFAULT NULL",
            "decision_at": "REAL DEFAULT NULL",
            "rule_version": "TEXT DEFAULT NULL",
            "observation_id": "INTEGER DEFAULT NULL",
            # Explicit evaluation contract.  scored_next remains NULL until an
            # ordered event feed can establish it reliably.
            "selected_team_outcome": "TEXT DEFAULT NULL",
            "selected_team_won": "INTEGER DEFAULT NULL",
            "equalized": "INTEGER DEFAULT NULL",
            "failed_to_equalize": "INTEGER DEFAULT NULL",
            "scored_next": "INTEGER DEFAULT NULL",
        }
        for name, definition in migrations.items():
            if name not in columns:
                await db.execute(f"ALTER TABLE anomalies ADD COLUMN {name} {definition}")
        await db.execute("DROP INDEX IF EXISTS idx_anomaly_match")
        await db.execute(
            "UPDATE anomalies SET detected_at_tr = datetime(created_at, '+3 hours') "
            "WHERE COALESCE(detected_at_tr, '') = '' "
            "AND COALESCE(created_at, '') != ''"
        )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_anomalies_result_status "
            "ON anomalies(result_status, deleted_at)"
        )
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS match_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                observed_at REAL NOT NULL,
                minute INTEGER,
                period TEXT,
                score_home INTEGER,
                score_away INTEGER,
                match_status TEXT,
                league TEXT,
                home_team TEXT,
                away_team TEXT,
                normalized_stats TEXT NOT NULL,
                missing_fields TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                validation_errors TEXT,
                provider TEXT NOT NULL,
                event_fetched_at REAL,
                stats_fetched_at REAL,
                decision_at REAL,
                rule_version TEXT,
                decision_outcome TEXT,
                decision_condition TEXT,
                selected_side TEXT,
                triggered_groups TEXT,
                decision_reasons TEXT,
                source_metadata TEXT,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_observations_event_time
                ON match_observations(event_id, observed_at, id);

            CREATE TABLE IF NOT EXISTS latest_match_states (
                event_id TEXT PRIMARY KEY,
                observation_id INTEGER NOT NULL,
                observed_at REAL NOT NULL,
                minute INTEGER,
                period TEXT,
                score_home INTEGER,
                score_away INTEGER,
                match_status TEXT,
                normalized_stats TEXT NOT NULL,
                missing_fields TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notification_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anomaly_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                message_text TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK(state IN ('PENDING', 'SENT', 'FAILED')),
                terminal INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL,
                claimed_at REAL,
                claim_expires_at REAL,
                claim_token TEXT,
                last_error TEXT,
                telegram_message_id INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                sent_at REAL,
                UNIQUE(anomaly_id, chat_id)
            );

            CREATE INDEX IF NOT EXISTS idx_notification_due
                ON notification_deliveries(state, terminal, next_attempt_at,
                                           claim_expires_at, created_at);
        """)
        await db.commit()


# ---- Immutable observation history ----


def _decode_json_column(value, fallback):
    if not isinstance(value, str):
        return value if value is not None else fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


async def store_match_observation(
    *,
    event_id: str,
    observed_at: float,
    minute: int | None,
    period: str | None,
    score_home: int | None,
    score_away: int | None,
    match_status: str | None,
    league: str | None,
    home_team: str | None,
    away_team: str | None,
    normalized_stats: dict,
    missing_fields: list[str],
    validation_status: str,
    validation_errors: list[str] | None = None,
    provider: str = "sofascore",
    event_fetched_at: float | None = None,
    stats_fetched_at: float | None = None,
    decision_at: float | None = None,
    rule_version: str | None = None,
    decision_outcome: str | None = None,
    decision_condition: str | None = None,
    selected_side: str | None = None,
    triggered_groups: list[str] | None = None,
    decision_reasons: list[str] | None = None,
    source_metadata: dict | None = None,
) -> int:
    """Append a lossless normalized observation and advance latest state.

    Historical rows are never updated.  A late-arriving older observation is
    retained but cannot move ``latest_match_states`` backwards.
    """
    if not event_id:
        raise ValueError("event_id is required")
    if validation_status not in {"VALID", "PARTIAL", "INVALID"}:
        raise ValueError("validation_status must be VALID, PARTIAL, or INVALID")

    created_at = time.time()
    stats_json = json.dumps(normalized_stats, separators=(",", ":"))
    missing_json = json.dumps(sorted(set(missing_fields)), separators=(",", ":"))
    validation_json = json.dumps(validation_errors or [], separators=(",", ":"))
    groups_json = (
        json.dumps(triggered_groups, separators=(",", ":"))
        if triggered_groups is not None else None
    )
    reasons_json = (
        json.dumps(decision_reasons, separators=(",", ":"))
        if decision_reasons is not None else None
    )
    source_json = (
        json.dumps(source_metadata, separators=(",", ":"))
        if source_metadata is not None else None
    )

    async with get_db(write=True) as db:
        cursor = await db.execute(
            """INSERT INTO match_observations (
                   event_id, observed_at, minute, period, score_home, score_away,
                   match_status, league, home_team, away_team, normalized_stats,
                   missing_fields, validation_status, validation_errors, provider,
                   event_fetched_at, stats_fetched_at, decision_at, rule_version,
                   decision_outcome, decision_condition, selected_side,
                   triggered_groups, decision_reasons, source_metadata, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, float(observed_at), minute, period, score_home, score_away,
                match_status, league, home_team, away_team, stats_json, missing_json,
                validation_status, validation_json, provider, event_fetched_at,
                stats_fetched_at, decision_at, rule_version, decision_outcome,
                decision_condition, selected_side, groups_json, reasons_json,
                source_json, created_at,
            ),
        )
        observation_id = int(cursor.lastrowid)
        await db.execute(
            """INSERT INTO latest_match_states (
                   event_id, observation_id, observed_at, minute, period,
                   score_home, score_away, match_status, normalized_stats,
                   missing_fields, validation_status, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_id) DO UPDATE SET
                   observation_id=excluded.observation_id,
                   observed_at=excluded.observed_at,
                   minute=excluded.minute,
                   period=excluded.period,
                   score_home=excluded.score_home,
                   score_away=excluded.score_away,
                   match_status=excluded.match_status,
                   normalized_stats=excluded.normalized_stats,
                   missing_fields=excluded.missing_fields,
                   validation_status=excluded.validation_status,
                   updated_at=excluded.updated_at
               WHERE excluded.observed_at > latest_match_states.observed_at
                  OR (excluded.observed_at = latest_match_states.observed_at
                      AND excluded.observation_id > latest_match_states.observation_id)""",
            (
                event_id, observation_id, float(observed_at), minute, period,
                score_home, score_away, match_status, stats_json, missing_json,
                validation_status, created_at,
            ),
        )
        await db.commit()
        return observation_id


def _observation_dict(row: aiosqlite.Row) -> dict:
    item = dict(row)
    for key, fallback in (
        ("normalized_stats", {}),
        ("missing_fields", []),
        ("validation_errors", []),
        ("triggered_groups", None),
        ("decision_reasons", None),
        ("source_metadata", None),
    ):
        if key in item:
            item[key] = _decode_json_column(item[key], fallback)
    return item


async def get_recent_observations(
    event_id: str,
    *,
    since: float | None = None,
    before: float | None = None,
    limit: int = 200,
) -> list[dict]:
    clauses = ["event_id = ?"]
    params: list = [event_id]
    if since is not None:
        clauses.append("observed_at >= ?")
        params.append(float(since))
    if before is not None:
        clauses.append("observed_at <= ?")
        params.append(float(before))
    params.append(max(1, min(int(limit), 5000)))
    async with get_db() as db:
        cursor = await db.execute(
            f"SELECT * FROM match_observations WHERE {' AND '.join(clauses)} "
            "ORDER BY observed_at DESC, id DESC LIMIT ?",
            params,
        )
        return [_observation_dict(row) for row in await cursor.fetchall()]


async def get_latest_match_state(event_id: str) -> dict | None:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT o.* FROM latest_match_states AS latest
               JOIN match_observations AS o ON o.id = latest.observation_id
               WHERE latest.event_id = ?""",
            (event_id,),
        )
        row = await cursor.fetchone()
        return _observation_dict(row) if row else None


async def cleanup_observations(retention_days: int) -> int:
    """Delete old unreferenced history while preserving signal/latest evidence."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    cutoff = time.time() - retention_days * 86400
    async with get_db(write=True) as db:
        cursor = await db.execute(
            """DELETE FROM match_observations AS o
               WHERE o.observed_at < ?
                 AND NOT EXISTS (
                     SELECT 1 FROM anomalies AS a WHERE a.observation_id = o.id
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM latest_match_states AS latest
                     WHERE latest.observation_id = o.id
                 )""",
            (cutoff,),
        )
        await db.commit()
        return cursor.rowcount


# ---- Notification delivery state ----


async def create_notification_delivery(
    anomaly_id: int,
    chat_id: str,
    message_text: str,
    *,
    now: float | None = None,
) -> tuple[dict, bool]:
    """Create one pending delivery per anomaly and recipient.

    Returns ``(delivery, created)``. Existing rows, including SENT rows, are
    returned unchanged and are never reset for another send.
    """
    timestamp = time.time() if now is None else float(now)
    async with get_db(write=True) as db:
        cursor = await db.execute(
            "SELECT * FROM notification_deliveries "
            "WHERE anomaly_id = ? AND chat_id = ?",
            (anomaly_id, str(chat_id)),
        )
        existing = await cursor.fetchone()
        if existing:
            return dict(existing), False

        cursor = await db.execute(
            """INSERT INTO notification_deliveries
               (anomaly_id, chat_id, message_text, state, terminal,
                attempt_count, created_at, updated_at)
               VALUES (?, ?, ?, 'PENDING', 0, 0, ?, ?)""",
            (anomaly_id, str(chat_id), message_text, timestamp, timestamp),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT * FROM notification_deliveries WHERE id = ?",
            (cursor.lastrowid,),
        )
        return dict(await cursor.fetchone()), True


async def record_notification_failure(
    delivery_id: int,
    error: str,
    *,
    next_retry_at: float | None = None,
    now: float | None = None,
) -> dict | None:
    """Record a failed attempt on the existing delivery row."""
    timestamp = time.time() if now is None else float(now)
    async with get_db(write=True) as db:
        await db.execute(
            """UPDATE notification_deliveries
               SET state = 'FAILED', terminal = 0,
                   attempt_count = COALESCE(attempt_count, 0) + 1,
                   last_error = ?, next_attempt_at = ?, updated_at = ?
               WHERE id = ? AND state != 'SENT'""",
            (str(error), next_retry_at, timestamp, delivery_id),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT * FROM notification_deliveries WHERE id = ?",
            (delivery_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def mark_notification_sent(
    delivery_id: int,
    *,
    telegram_message_id: int | None = None,
    sent_at: float | None = None,
    now: float | None = None,
) -> dict | None:
    """Mark an existing delivery SENT and clear retry state."""
    timestamp = time.time() if now is None else float(now)
    delivered_at = timestamp if sent_at is None else float(sent_at)
    async with get_db(write=True) as db:
        await db.execute(
            """UPDATE notification_deliveries
               SET state = 'SENT', terminal = 1, last_error = NULL,
                   next_attempt_at = NULL, sent_at = ?,
                   telegram_message_id = COALESCE(?, telegram_message_id),
                   updated_at = ?
               WHERE id = ?""",
            (delivered_at, telegram_message_id, timestamp, delivery_id),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT * FROM notification_deliveries WHERE id = ?",
            (delivery_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_due_notification_deliveries(
    *,
    now: float | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return pending/failed deliveries whose retry time has arrived."""
    timestamp = time.time() if now is None else float(now)
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT * FROM notification_deliveries
               WHERE terminal = 0
                 AND state IN ('PENDING', 'FAILED')
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY created_at, id
               LIMIT ?""",
            (timestamp, max(1, min(int(limit), 5000))),
        )
        return [dict(row) for row in await cursor.fetchall()]


# ---- Anomaly CRUD ----


async def insert_anomaly(
    match_id: str, home_team: str, away_team: str,
    score_home: int, score_away: int, minute: int,
    league: str, condition_type: str,
    triggered_rules: list[str], stats_snapshot: dict,
    *,
    selected_side: str | None = None,
    triggered_groups: list[str] | None = None,
    missing_fields: list[str] | None = None,
    stats_period: str | None = None,
    event_fetched_at: float | None = None,
    stats_fetched_at: float | None = None,
    decision_at: float | None = None,
    rule_version: str | None = None,
    observation_id: int | None = None,
) -> tuple[int | None, bool, int]:
    """Insert a first-signal record without overwriting an existing signal."""
    async with get_db(write=True) as db:
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
                    alert_number, detected_at_tr, dominant_side, status,
                    selected_side, triggered_groups, missing_fields, stats_period,
                    event_fetched_at, stats_fetched_at, decision_at, rule_version,
                    observation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?)""",
                (
                    match_id, home_team, away_team, score_home, score_away,
                    minute, league, condition_type,
                    json.dumps(triggered_rules), json.dumps(stats_snapshot),
                    alert_number, turkey_now_str(),
                    selected_side if selected_side is not None else dominant_side,
                    status,
                    selected_side,
                    json.dumps(triggered_groups) if triggered_groups is not None else None,
                    json.dumps(missing_fields) if missing_fields is not None else None,
                    stats_period, event_fetched_at, stats_fetched_at, decision_at,
                    rule_version, observation_id,
                ),
            )
            await db.commit()
            return cursor.lastrowid, True, alert_number
        except Exception:
            logger.exception("Could not save anomaly for match %s", match_id)
            return None, False, 0


async def get_anomalies(status_filter: str | None = None, limit: int = 200):
    async with get_db() as db:
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
    """Return archived signals, optionally keeping one latest signal per match.

    ``hide_unique`` is kept as the public API name for backwards compatibility;
    in the dashboard it means "hide duplicate signals".  The result filter is
    deliberately applied before ranking so, for example, the successful view
    can retain one successful signal for every match that has one.
    """
    async with get_db() as db:
        clauses = ["deleted_at IS NOT NULL"]
        params: list = []
        if result_filter:
            clauses.append("result_status = ?")
            params.append(result_filter)
        where = " AND ".join(clauses)
        unique_clause = "WHERE match_row_number = 1" if hide_unique else ""
        cursor = await db.execute(
            f"""WITH filtered AS (
                    SELECT a.* FROM anomalies AS a WHERE {where}
                ), ranked AS (
                    SELECT filtered.*,
                           COUNT(*) OVER (PARTITION BY match_id) AS match_signal_count,
                           ROW_NUMBER() OVER (
                               PARTITION BY match_id
                               ORDER BY COALESCE(alert_number, 1) DESC, id DESC
                           ) AS match_row_number
                    FROM filtered
                )
                SELECT * FROM ranked
                {unique_clause}
                ORDER BY deleted_at DESC LIMIT ?""",
            params + [limit],
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_deleted_anomaly_summary(
    result_filter: str | None = None,
    hide_unique: bool = False,
) -> dict:
    async with get_db() as db:
        clauses = ["deleted_at IS NOT NULL"]
        params: list = []
        if result_filter:
            clauses.append("result_status = ?")
            params.append(result_filter)
        where = " AND ".join(clauses)
        unique_clause = "WHERE match_row_number = 1" if hide_unique else ""
        cursor = await db.execute(
            f"""WITH filtered AS (
                    SELECT a.* FROM anomalies AS a WHERE {where}
                ), ranked AS (
                    SELECT filtered.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY match_id
                               ORDER BY COALESCE(alert_number, 1) DESC, id DESC
                           ) AS match_row_number
                    FROM filtered
                ), summarized AS (
                    SELECT * FROM ranked {unique_clause}
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
               FROM summarized""",
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


async def get_pending_anomaly_match_ids(limit: int = 50) -> list[str]:
    """Claim a bounded, least-recently-checked batch, including trash.

    Mark attempts before network I/O so unavailable events cannot starve the
    rest of the backlog. Initially unchecked matches are processed oldest first.
    """
    async with get_db(write=True) as db:
        cursor = await db.execute(
            "SELECT match_id FROM anomalies "
            "WHERE COALESCE(result_status, 'pending') = 'pending' "
            "AND final_score_home IS NULL AND final_score_away IS NULL "
            "GROUP BY match_id ORDER BY MAX(result_checked_at), MIN(id) LIMIT ?",
            (max(1, min(limit, 50)),),
        )
        match_ids = [str(row["match_id"]) for row in await cursor.fetchall()]
        await db.executemany(
            "UPDATE anomalies SET result_checked_at = strftime('%Y-%m-%d %H:%M:%f', 'now') "
            "WHERE match_id = ? AND COALESCE(result_status, 'pending') = 'pending'",
            [(match_id,) for match_id in match_ids],
        )
        await db.commit()
        return match_ids


async def finalize_match_anomalies(
    match_id: str, final_score_home: int, final_score_away: int
) -> int:
    """Grade every pending signal for a finished match and archive active rows."""
    async with get_db(write=True) as db:
        cursor = await db.execute(
            "SELECT id, dominant_side, selected_side FROM anomalies "
            "WHERE match_id = ? AND COALESCE(result_status, 'pending') = 'pending'",
            (match_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0

        now_tr = turkey_now_str()
        for row in rows:
            selected_side = (
                row["selected_side"]
                if row["selected_side"] is not None
                else row["dominant_side"]
            )
            result_status = evaluate_signal_result(
                selected_side, final_score_home, final_score_away
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
    async with get_db(write=True) as db:
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
    async with get_db(write=True) as db:
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
    async with get_db(write=True) as db:
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"UPDATE anomalies SET deleted_at = datetime('now'), deletion_reason = 'manual' "
            f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            ids,
        )
        await db.commit()


async def soft_delete_all_anomalies():
    """Move all non-deleted anomalies to the trash."""
    async with get_db(write=True) as db:
        await db.execute(
            "UPDATE anomalies SET deleted_at = datetime('now'), deletion_reason = 'manual' "
            "WHERE deleted_at IS NULL"
        )
        await db.commit()


async def restore_anomalies(ids: list[int]):
    if not ids:
        return
    async with get_db(write=True) as db:
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
    async with get_db(write=True) as db:
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"DELETE FROM anomalies WHERE id IN ({placeholders})", ids
        )
        await db.commit()


async def purge_deleted_anomalies():
    """Permanently delete every row currently in the trash."""
    async with get_db(write=True) as db:
        await db.execute("DELETE FROM anomalies WHERE deleted_at IS NOT NULL")
        await db.commit()


async def clear_anomalies():
    """Permanently delete every anomaly (active and trashed)."""
    async with get_db(write=True) as db:
        await db.execute("DELETE FROM anomalies")
        await db.commit()


async def mark_notified(anomaly_id: int):
    async with get_db(write=True) as db:
        await db.execute(
            "UPDATE anomalies SET notified = 1 WHERE id = ?", (anomaly_id,)
        )
        await db.commit()


async def clear_database():
    async with get_db(write=True) as db:
        await db.execute("DELETE FROM anomalies")
        await db.execute("DELETE FROM anomaly_match_actions")
        await db.execute("DELETE FROM upcoming_matches")
        await db.commit()


async def close_db():
    """Compatibility shutdown hook; each operation closes its own connection."""


# ---- Upcoming Matches CRUD ----

async def upsert_upcoming_matches(matches: list[dict], scan_date: str) -> int:
    """Insert or update the rolling 24-hour fixture snapshot."""
    async with get_db(write=True) as db:
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
    async with get_db() as db:
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
    async with get_db(write=True) as db:
        await db.execute(
            "UPDATE upcoming_matches SET status = ? WHERE id = ?", (status, match_id)
        )
        await db.commit()


async def bulk_update_upcoming_status(ids: list[int], status: str):
    async with get_db(write=True) as db:
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"UPDATE upcoming_matches SET status = ? WHERE id IN ({placeholders})",
            [status] + ids,
        )
        await db.commit()


async def delete_upcoming_matches(ids: list[int]):
    async with get_db(write=True) as db:
        placeholders = ",".join("?" for _ in ids)
        await db.execute(f"DELETE FROM upcoming_matches WHERE id IN ({placeholders})", ids)
        await db.commit()


async def clear_upcoming_matches():
    async with get_db(write=True) as db:
        await db.execute("DELETE FROM upcoming_matches")
        await db.commit()


async def mark_upcoming_anomaly(event_ids: list[str], scan_date: str):
    """Mark rolling-snapshot fixtures for which a live anomaly was detected."""
    if not event_ids:
        return
    async with get_db(write=True) as db:
        placeholders = ",".join("?" for _ in event_ids)
        await db.execute(
            f"UPDATE upcoming_matches SET has_anomaly = 1 "
            f"WHERE event_id IN ({placeholders}) AND scan_date = ?",
            event_ids + [scan_date],
        )
        await db.commit()
