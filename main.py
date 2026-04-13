"""
FastAPI application — serves the dashboard and API endpoints.
Starts background workers on startup via APScheduler.
"""

import logging
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from db import (
    init_db, close_db, get_anomalies, update_anomaly_status,
    bulk_update_anomaly_status, delete_anomalies, clear_anomalies,
    get_analyses, delete_analyses, clear_analyses,
    get_upcoming_matches_db, update_upcoming_match_status,
    bulk_update_upcoming_status, delete_upcoming_matches, clear_upcoming_matches,
    clear_database,
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
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "No ids provided"}, status_code=400)
    await delete_anomalies(ids)
    return {"ok": True}


@app.post("/api/anomalies/clear")
async def api_clear_anomalies():
    await clear_anomalies()
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


# ---- Manual triggers (for testing) ----

@app.post("/api/trigger/live-scan")
async def trigger_live_scan():
    import asyncio
    asyncio.create_task(live_scan())
    return {"ok": True, "message": "Live scan triggered"}


@app.post("/api/trigger/upcoming-scan")
async def trigger_upcoming_scan():
    import asyncio
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
