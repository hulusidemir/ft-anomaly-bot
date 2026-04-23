"""
FastAPI application — serves the dashboard and API endpoints.
Starts background workers on startup via APScheduler.
"""

import asyncio
import logging
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from db import (
    init_db, close_db, get_anomalies, update_anomaly_status,
    bulk_update_anomaly_status, delete_anomalies,
    soft_delete_anomalies, soft_delete_all_anomalies,
    restore_anomalies, get_deleted_anomalies, purge_deleted_anomalies,
    get_analyses, delete_analyses, clear_analyses,
    get_upcoming_matches_db, update_upcoming_match_status,
    bulk_update_upcoming_status, delete_upcoming_matches, clear_upcoming_matches,
    clear_database,
    get_live_actions, set_live_action, bulk_set_live_actions,
)
from workers import live_scan, upcoming_scan
from scraper import scraper
from notifier import send_telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    logger.info("Database initialized")

    # Worker 1: live scan every N seconds
    scheduler.add_job(
        live_scan,
        "interval",
        seconds=config.SCAN_INTERVAL_SECONDS,
        id="live_scan",
        max_instances=1,
        misfire_grace_time=30,
    )

    # Worker 2: upcoming analysis at 07:00 and 19:00 Turkey time
    scheduler.add_job(
        upcoming_scan,
        "cron",
        hour=7,
        minute=0,
        timezone="Europe/Istanbul",
        args=["morning"],
        id="upcoming_morning",
        max_instances=1,
    )
    scheduler.add_job(
        upcoming_scan,
        "cron",
        hour=19,
        minute=0,
        timezone="Europe/Istanbul",
        args=["evening"],
        id="upcoming_evening",
        max_instances=1,
    )

    scheduler.start()
    logger.info(
        f"Scheduler started — live scan every {config.SCAN_INTERVAL_SECONDS}s, "
        f"upcoming at 07:00/19:00 Istanbul"
    )

    # Startup notification
    await send_telegram(
        "✅ <b>Anomali Bot başlatıldı!</b>\n\n"
        f"⏱ Canlı tarama: her {config.SCAN_INTERVAL_SECONDS} saniye\n"
        "📅 Maç analizi: 07:00 / 19:00 (İstanbul)\n"
        f"🌐 Dashboard: http://{config.HOST}:{config.PORT}"
    )

    yield

    # Shutdown notification
    await send_telegram(
        "🛑 <b>Anomali Bot durduruldu.</b>\n"
        "Sistem kapatıldı."
    )

    # Shutdown
    scheduler.shutdown(wait=False)
    await scraper.close()
    await close_db()
    logger.info("Shutdown complete")


app = FastAPI(title="Football Anomaly Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ---- Favicon ----

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("static/favicon.svg", media_type="image/svg+xml")


# ---- Dashboard ----

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ---- API Endpoints ----

@app.get("/api/anomalies")
async def api_anomalies(status: str | None = None):
    rows = await get_anomalies(status_filter=status)
    # Parse JSON strings for frontend
    for row in rows:
        if isinstance(row.get("triggered_rules"), str):
            row["triggered_rules"] = json.loads(row["triggered_rules"])
        if isinstance(row.get("stats_snapshot"), str):
            row["stats_snapshot"] = json.loads(row["stats_snapshot"])
    return rows


@app.post("/api/anomalies/{anomaly_id}/status")
async def api_update_status(anomaly_id: int, request: Request):
    body = await request.json()
    status = body.get("status")
    if status not in ("new", "bet_placed", "ignored", "following"):
        return JSONResponse({"error": "Invalid status"}, status_code=400)
    await update_anomaly_status(anomaly_id, status)
    return {"ok": True}


@app.post("/api/anomalies/bulk-status")
async def api_bulk_status(request: Request):
    body = await request.json()
    ids = body.get("ids", [])
    status = body.get("status")
    if not ids or status not in ("new", "bet_placed", "ignored", "following"):
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    await bulk_update_anomaly_status(ids, status)
    return {"ok": True}


@app.post("/api/anomalies/delete")
async def api_delete_anomalies(request: Request):
    """Soft delete: move anomalies to the trash."""
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "No ids provided"}, status_code=400)
    await soft_delete_anomalies(ids)
    return {"ok": True}


@app.post("/api/anomalies/clear")
async def api_clear_anomalies():
    """Soft delete: send every active anomaly to the trash."""
    await soft_delete_all_anomalies()
    return {"ok": True}


@app.get("/api/anomalies/deleted")
async def api_get_deleted_anomalies():
    rows = await get_deleted_anomalies()
    for row in rows:
        if isinstance(row.get("triggered_rules"), str):
            row["triggered_rules"] = json.loads(row["triggered_rules"])
        if isinstance(row.get("stats_snapshot"), str):
            row["stats_snapshot"] = json.loads(row["stats_snapshot"])
    return rows


@app.post("/api/anomalies/restore")
async def api_restore_anomalies(request: Request):
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "No ids provided"}, status_code=400)
    await restore_anomalies(ids)
    return {"ok": True}


@app.post("/api/anomalies/purge")
async def api_purge_anomalies(request: Request):
    """Permanently delete specific trashed anomalies."""
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "No ids provided"}, status_code=400)
    await delete_anomalies(ids)
    return {"ok": True}


@app.post("/api/anomalies/purge-all")
async def api_purge_all_anomalies():
    """Permanently delete everything currently in the trash."""
    await purge_deleted_anomalies()
    return {"ok": True}


@app.get("/api/analyses")
async def api_analyses():
    return await get_analyses()


@app.post("/api/analyses/delete")
async def api_delete_analyses(request: Request):
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "No ids provided"}, status_code=400)
    await delete_analyses(ids)
    return {"ok": True}


@app.post("/api/analyses/clear")
async def api_clear_analyses():
    await clear_analyses()
    return {"ok": True}


# ---- Upcoming Matches Endpoints ----

@app.get("/api/upcoming")
async def api_upcoming(date: str | None = None, status: str | None = None):
    rows = await get_upcoming_matches_db(scan_date=date, status_filter=status)
    return rows


@app.post("/api/upcoming/{match_id}/status")
async def api_update_upcoming_status(match_id: int, request: Request):
    body = await request.json()
    status = body.get("status")
    if status not in ("new", "following", "ignored"):
        return JSONResponse({"error": "Invalid status"}, status_code=400)
    await update_upcoming_match_status(match_id, status)
    return {"ok": True}


@app.post("/api/upcoming/bulk-status")
async def api_bulk_upcoming_status(request: Request):
    body = await request.json()
    ids = body.get("ids", [])
    status = body.get("status")
    if not ids or status not in ("new", "following", "ignored"):
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    await bulk_update_upcoming_status(ids, status)
    return {"ok": True}


@app.post("/api/upcoming/delete")
async def api_delete_upcoming(request: Request):
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "No ids provided"}, status_code=400)
    await delete_upcoming_matches(ids)
    return {"ok": True}


@app.post("/api/upcoming/clear")
async def api_clear_upcoming():
    await clear_upcoming_matches()
    return {"ok": True}


@app.post("/api/database/clear")
async def api_clear_database():
    await clear_database()
    return {"ok": True}


@app.get("/api/status")
async def api_status():
    """Health check and scheduler info."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
        })
    return {"status": "running", "scheduler_jobs": jobs}


# ---- Live Matches Endpoints ----

# Short-TTL in-memory caches so the dashboard tab is fast and we don't hammer
# Sofascore's rate limit. Sofascore's live endpoint already has heavy bot
# protection; re-using the last scrape for ~20s is safe.
_live_list_cache: dict = {"matches": [], "ts": 0.0, "has_value": False}
_live_list_lock = asyncio.Lock()

_live_details_cache: dict[str, dict] = {}  # event_id -> {"data": dict, "ts": float}
_live_details_locks: dict[str, asyncio.Lock] = {}

LIVE_LIST_TTL = 20.0
LIVE_DETAILS_TTL = 45.0


async def _get_live_matches_cached(retries: int = 5):
    async with _live_list_lock:
        now = time.monotonic()
        if _live_list_cache["has_value"] and (now - _live_list_cache["ts"]) < LIVE_LIST_TTL:
            return _live_list_cache["matches"], None, False

        fresh = await scraper.get_live_matches(retries=retries)
        fetch_error = scraper.last_live_fetch_error
        if fetch_error:
            if _live_list_cache["has_value"]:
                return _live_list_cache["matches"], fetch_error, True
            return [], fetch_error, False

        if fresh or not fetch_error:
            _live_list_cache["matches"] = fresh
            _live_list_cache["ts"] = now
            _live_list_cache["has_value"] = True
    return _live_list_cache["matches"], None, False


@app.get("/api/live-matches")
async def api_live_matches():
    """Return all currently-live football matches enriched with user action status."""
    matches, fetch_error, stale = await _get_live_matches_cached(retries=2)
    if fetch_error and not stale:
        detail = fetch_error.get("message", "Canlı maç listesi alınamadı")
        status = fetch_error.get("status")
        suffix = f" (HTTP {status})" if status else ""
        return JSONResponse(
            {"error": f"Sofascore canlı maç listesi alınamadı: {detail}{suffix}"},
            status_code=502,
        )

    event_ids = [m.event_id for m in matches]
    actions = await get_live_actions(event_ids) if event_ids else {}

    payload = []
    for m in matches:
        payload.append({
            "event_id": m.event_id,
            "home_team": m.home_team,
            "away_team": m.away_team,
            "score_home": m.score_home,
            "score_away": m.score_away,
            "minute": m.minute,
            "league": m.league,
            "status_desc": m.status_desc,
            "status": actions.get(m.event_id, "new"),
        })
    return payload


@app.get("/api/live-matches/{event_id}/details")
async def api_live_match_details(event_id: str):
    """Return enriched stats/form/votes/odds for a single live match (cached 45s)."""
    now = time.monotonic()
    cached = _live_details_cache.get(event_id)
    if cached and (now - cached["ts"]) < LIVE_DETAILS_TTL:
        return cached["data"]

    lock = _live_details_locks.setdefault(event_id, asyncio.Lock())
    async with lock:
        cached = _live_details_cache.get(event_id)
        if cached and (time.monotonic() - cached["ts"]) < LIVE_DETAILS_TTL:
            return cached["data"]

        details = await scraper.get_live_match_details(event_id)
        _live_details_cache[event_id] = {"data": details, "ts": time.monotonic()}

        # Opportunistic cleanup: drop old entries to stop unbounded growth.
        if len(_live_details_cache) > 400:
            cutoff = time.monotonic() - LIVE_DETAILS_TTL * 4
            stale = [k for k, v in _live_details_cache.items() if v["ts"] < cutoff]
            for k in stale:
                _live_details_cache.pop(k, None)
                _live_details_locks.pop(k, None)

        return details


@app.get("/api/live-matches-2")
async def api_live_matches_2():
    """Manual live-match research view: fresh live list only.

    Details are intentionally loaded per match from the dashboard so this
    request cannot hang while dozens of Sofascore detail endpoints are fetched.
    """
    matches = await scraper.get_live_matches(retries=1)
    fetch_error = scraper.last_live_fetch_error
    if fetch_error:
        detail = fetch_error.get("message", "Canlı maç listesi alınamadı")
        status = fetch_error.get("status")
        suffix = f" (HTTP {status})" if status else ""
        return JSONResponse(
            {"error": f"Sofascore canlı maç listesi alınamadı: {detail}{suffix}"},
            status_code=502,
        )

    event_ids = [m.event_id for m in matches]
    actions = await get_live_actions(event_ids) if event_ids else {}

    payload = []
    for m in matches:
        payload.append({
            "event_id": m.event_id,
            "home_team": m.home_team,
            "away_team": m.away_team,
            "score_home": m.score_home,
            "score_away": m.score_away,
            "minute": m.minute,
            "league": m.league,
            "status_desc": m.status_desc,
            "status": actions.get(m.event_id, "new"),
            "details": None,
        })
    return payload


@app.get("/api/live-matches-2/{event_id}/stats")
async def api_live_match_2_stats(event_id: str):
    """Return only match statistics for the fast text-focused live-2 view."""
    try:
        stats = await scraper.get_match_statistics(event_id)
    except Exception as e:
        logger.error("Live-2 stats failed for event %s: %s", event_id, e, exc_info=True)
        return {"stats": None, "error": "İstatistik servisi hata verdi"}

    if not stats:
        return {"stats": None, "error": ""}
    return {"stats": stats.to_dict(), "error": ""}


@app.post("/api/live-matches/{event_id}/status")
async def api_live_match_status(event_id: str, request: Request):
    body = await request.json()
    status = body.get("status")
    if status not in ("new", "bet_placed", "ignored", "following"):
        return JSONResponse({"error": "Invalid status"}, status_code=400)
    await set_live_action(event_id, status)
    return {"ok": True}


@app.post("/api/live-matches/bulk-status")
async def api_live_match_bulk_status(request: Request):
    body = await request.json()
    event_ids = body.get("event_ids", [])
    status = body.get("status")
    if not event_ids or status not in ("new", "bet_placed", "ignored", "following"):
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    await bulk_set_live_actions(event_ids, status)
    return {"ok": True}


# ---- Manual triggers (for testing) ----

@app.post("/api/trigger/live-scan")
async def trigger_live_scan():
    asyncio.create_task(live_scan())
    return {"ok": True, "message": "Live scan triggered"}


@app.post("/api/trigger/upcoming-scan")
async def trigger_upcoming_scan():
    asyncio.create_task(upcoming_scan("manual"))
    return {"ok": True, "message": "Upcoming scan triggered"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
        log_level="info",
    )
