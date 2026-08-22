"""
FastAPI application — serves the dashboard and API endpoints.
Starts background workers on startup via APScheduler.
"""

import asyncio
import logging
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime

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
    get_deleted_anomaly_summary,
    get_upcoming_matches_db, update_upcoming_match_status,
    bulk_update_upcoming_status, delete_upcoming_matches, clear_upcoming_matches,
    clear_database,
)
from workers import anomaly_scan, refresh_upcoming_matches, finished_match_scan
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

    # Worker 1: anomaly scan every N seconds
    scheduler.add_job(
        anomaly_scan,
        "interval",
        seconds=config.SCAN_INTERVAL_SECONDS,
        id="anomaly_scan",
        max_instances=1,
        misfire_grace_time=30,
    )

    # Worker 2: finalize completed signal matches every 30 minutes.
    scheduler.add_job(
        finished_match_scan,
        "interval",
        minutes=config.FINISHED_SCAN_INTERVAL_MINUTES,
        id="finished_match_scan",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        next_run_time=datetime.now(config.TZ_TURKEY),
    )

    scheduler.start()
    logger.info(
        f"Scheduler started — anomaly scan every {config.SCAN_INTERVAL_SECONDS}s, "
        f"finished-match scan every {config.FINISHED_SCAN_INTERVAL_MINUTES}m"
    )

    # Startup notification
    await send_telegram(
        "✅ <b>Anomali Bot başlatıldı!</b>\n\n"
        f"⏱ Anomali taraması: her {config.SCAN_INTERVAL_SECONDS} saniye\n"
        f"🏁 Biten maç kontrolü: her {config.FINISHED_SCAN_INTERVAL_MINUTES} dakika\n"
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
    updated = await update_anomaly_status(anomaly_id, status)
    return {"ok": True, "updated": updated}


@app.post("/api/anomalies/bulk-status")
async def api_bulk_status(request: Request):
    body = await request.json()
    ids = body.get("ids", [])
    status = body.get("status")
    if not ids or status not in ("new", "bet_placed", "ignored", "following"):
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    updated = await bulk_update_anomaly_status(ids, status)
    return {"ok": True, "updated": updated}


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
async def api_get_deleted_anomalies(result: str | None = None):
    if result not in (None, "successful", "failed", "pending", "unresolved"):
        return JSONResponse({"error": "Invalid result filter"}, status_code=400)
    rows = await get_deleted_anomalies(result_filter=result)
    for row in rows:
        if isinstance(row.get("triggered_rules"), str):
            row["triggered_rules"] = json.loads(row["triggered_rules"])
        if isinstance(row.get("stats_snapshot"), str):
            row["stats_snapshot"] = json.loads(row["stats_snapshot"])
    return {"items": rows, "summary": await get_deleted_anomaly_summary()}


@app.post("/api/anomalies/restore")
async def api_restore_anomalies(request: Request):
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "No ids provided"}, status_code=400)
    restored = await restore_anomalies(ids)
    return {"ok": True, "restored": restored}


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


# ---- Upcoming Matches Endpoints ----

@app.get("/api/upcoming")
async def api_upcoming(date: str | None = None, status: str | None = None):
    min_start_time = None
    if date is None:
        date = datetime.now(config.TZ_TURKEY).strftime("%Y-%m-%d")
        min_start_time = int(time.time()) - scraper.UPCOMING_START_GRACE_SECONDS
    rows = await get_upcoming_matches_db(
        scan_date=date,
        status_filter=status,
        min_start_time=min_start_time,
        limit=2000,
    )
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


# ---- Anomaly Match Details ----

_anomaly_details_cache: dict[str, dict] = {}
_anomaly_details_locks: dict[str, asyncio.Lock] = {}
ANOMALY_DETAILS_TTL = 45.0


@app.get("/api/anomalies/{event_id}/details")
async def api_anomaly_match_details(event_id: str):
    """Return enriched stats/form/votes/odds for an anomaly event."""
    now = time.monotonic()
    cached = _anomaly_details_cache.get(event_id)
    if cached and (now - cached["ts"]) < ANOMALY_DETAILS_TTL:
        return cached["data"]

    lock = _anomaly_details_locks.setdefault(event_id, asyncio.Lock())
    async with lock:
        cached = _anomaly_details_cache.get(event_id)
        if cached and (time.monotonic() - cached["ts"]) < ANOMALY_DETAILS_TTL:
            return cached["data"]

        details = await scraper.get_anomaly_match_details(event_id)
        _anomaly_details_cache[event_id] = {"data": details, "ts": time.monotonic()}

        # Opportunistic cleanup: drop old entries to stop unbounded growth.
        if len(_anomaly_details_cache) > 400:
            cutoff = time.monotonic() - ANOMALY_DETAILS_TTL * 4
            stale = [k for k, v in _anomaly_details_cache.items() if v["ts"] < cutoff]
            for k in stale:
                _anomaly_details_cache.pop(k, None)
                _anomaly_details_locks.pop(k, None)

        return details


# ---- Manual triggers (for testing) ----

@app.post("/api/trigger/upcoming-scan")
async def trigger_upcoming_scan():
    result = await refresh_upcoming_matches()
    if not result.get("ok"):
        status_code = 409 if result.get("busy") else 502
        return JSONResponse(
            {"error": result.get("error") or "Upcoming matches could not be fetched"},
            status_code=status_code,
        )
    return result


@app.post("/api/trigger/finished-scan")
async def trigger_finished_scan():
    result = await finished_match_scan()
    if not result.get("ok"):
        status_code = 409 if result.get("busy") else 500
        return JSONResponse(
            {"error": result.get("error") or "Finished-match scan is busy"},
            status_code=status_code,
        )
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
        log_level="info",
    )
