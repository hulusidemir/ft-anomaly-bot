"""
Background workers:
  1. Anomaly scanner — runs every SCAN_INTERVAL_SECONDS
  2. Finished-match scanner — grades completed signal matches

Upcoming fixtures can still be refreshed manually from the dashboard. They are
not scheduled, analyzed by an AI model, or sent to Telegram.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from scraper import scraper
from detector import detect_anomalies
from config import TZ_TURKEY
from notifier import send_telegram, format_anomaly_message
from db import (
    insert_anomaly, mark_notified,
    upsert_upcoming_matches, mark_upcoming_anomaly,
    get_pending_anomaly_match_ids, finalize_match_anomalies,
)

logger = logging.getLogger(__name__)

_scan_lock = asyncio.Lock()
_upcoming_lock = asyncio.Lock()
_finished_match_lock = asyncio.Lock()


async def anomaly_scan():
    """Worker 1: Scan current matches for anomalies."""
    if _scan_lock.locked():
        logger.debug("Live scan already running, skipping")
        return

    async with _scan_lock:
        logger.info("Starting anomaly scan...")
        try:
            matches = await scraper.get_live_matches()
            logger.info(f"Found {len(matches)} live matches")

            if matches:
                minutes = [m.minute for m in matches]
                logger.info(
                    f"Minute range: {min(minutes)}-{max(minutes)}, "
                    f"distribution: {sorted(set(minutes))[:10]}"
                )

            # Filter: 30-85 min window.
            # Lower bound 30: stats are too sparse before ~30' for reliable
            #   ratio-based rules (cold starts, tactical probing).
            # Upper bound 85: catch late drama that 80 missed, but clip stoppage
            #   noise (90+) that rarely has room for follow-through.
            eligible = [m for m in matches if 30 <= m.minute <= 85]
            logger.info(f"Eligible matches (30-85 min): {len(eligible)}")

            if not eligible:
                return

            # Fetch stats concurrently (semaphore in scraper handles rate limiting)
            stats_tasks = [
                scraper.get_match_statistics(m.event_id) for m in eligible
            ]
            stats_results = await asyncio.gather(*stats_tasks, return_exceptions=True)

            stats_ok = sum(1 for s in stats_results if s is not None and not isinstance(s, Exception))
            logger.info(f"Stats fetched: {stats_ok}/{len(eligible)} successful")

            anomaly_count = 0
            anomaly_event_ids: set[str] = set()
            for match, stats_result in zip(eligible, stats_results):
                if isinstance(stats_result, Exception) or stats_result is None:
                    logger.debug(
                        f"No stats for {match.home_team} vs {match.away_team} "
                        f"(id={match.event_id})"
                    )
                    continue

                anomalies = detect_anomalies(match, stats_result)
                if anomalies:
                    anomaly_event_ids.add(match.event_id)
                for condition_type, rules in anomalies:
                    stats_dict = stats_result.to_dict()
                    row_id, is_new, alert_number = await insert_anomaly(
                        match_id=match.event_id,
                        home_team=match.home_team,
                        away_team=match.away_team,
                        score_home=match.score_home,
                        score_away=match.score_away,
                        minute=match.minute,
                        league=match.league,
                        condition_type=condition_type,
                        triggered_rules=rules,
                        stats_snapshot=stats_dict,
                    )

                    if row_id and is_new:
                        anomaly_count += 1
                        # Send Telegram notification
                        msg = format_anomaly_message(
                            home_team=match.home_team,
                            away_team=match.away_team,
                            score_home=match.score_home,
                            score_away=match.score_away,
                            minute=match.minute,
                            league=match.league,
                            condition_type=condition_type,
                            triggered_rules=rules,
                            stats=stats_dict,
                            alert_number=alert_number,
                        )
                        sent = await send_telegram(msg)
                        if sent is not None:
                            await mark_notified(row_id)

            if anomaly_count > 0:
                logger.info(f"Detected {anomaly_count} new anomalies")
                if anomaly_event_ids:
                    # Mark the match both under today's and yesterday's scan_date
                    # so late-night kick-offs that span midnight still get tagged.
                    now_tr = datetime.now(TZ_TURKEY)
                    today = now_tr.strftime("%Y-%m-%d")
                    yesterday = (now_tr - timedelta(days=1)).strftime("%Y-%m-%d")
                    ids = list(anomaly_event_ids)
                    await mark_upcoming_anomaly(ids, today)
                    await mark_upcoming_anomaly(ids, yesterday)
            else:
                logger.debug("No new anomalies found")

        except Exception as e:
            logger.error(f"Live scan error: {e}", exc_info=True)


async def finished_match_scan() -> dict:
    """Check pending signal matches, grade finished ones, and archive them."""
    if _finished_match_lock.locked():
        logger.debug("Finished-match scan already running, skipping")
        return {"ok": False, "busy": True, "checked": 0, "archived": 0}

    async with _finished_match_lock:
        try:
            event_ids = await get_pending_anomaly_match_ids()
            if not event_ids:
                logger.debug("No pending anomaly matches to finalize")
                return {"ok": True, "checked": 0, "matches_finished": 0, "archived": 0}

            logger.info("Checking %s anomaly matches for final results", len(event_ids))
            results = await asyncio.gather(
                *(scraper.get_match_result(event_id) for event_id in event_ids),
                return_exceptions=True,
            )

            archived = 0
            matches_finished = 0
            errors = 0
            for event_id, result in zip(event_ids, results):
                if isinstance(result, Exception):
                    errors += 1
                    logger.warning(
                        "Result check failed for event %s: %s", event_id, result
                    )
                    continue
                if (
                    result is None
                    or not result.is_finished
                    or result.score_home is None
                    or result.score_away is None
                ):
                    continue
                matches_finished += 1
                archived += await finalize_match_anomalies(
                    event_id, result.score_home, result.score_away
                )

            logger.info(
                "Finished-match scan complete: checked=%s finished=%s archived_signals=%s errors=%s",
                len(event_ids), matches_finished, archived, errors,
            )
            return {
                "ok": True,
                "checked": len(event_ids),
                "matches_finished": matches_finished,
                "archived": archived,
                "errors": errors,
            }
        except Exception as exc:
            logger.error("Finished-match scan error: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc), "checked": 0, "archived": 0}


async def refresh_upcoming_matches() -> dict:
    """Fetch and store upcoming matches after an explicit dashboard request."""
    if _upcoming_lock.locked():
        logger.debug("Upcoming scan already running, skipping")
        return {"ok": False, "busy": True, "error": "Upcoming scan already running"}

    async with _upcoming_lock:
        logger.info("Starting manual upcoming match refresh...")
        try:
            matches = await scraper.get_upcoming_matches()
            logger.info(f"Found {len(matches)} upcoming matches")

            if not matches:
                fetch_error = scraper.last_fetch_error or {}
                return {
                    "ok": not bool(fetch_error),
                    "count": 0,
                    "saved": 0,
                    "error": fetch_error.get("message"),
                }

            # ── 0. Save matches to DB ──
            scan_date = datetime.now(TZ_TURKEY).strftime("%Y-%m-%d")
            match_dicts = [
                {
                    "event_id": m.event_id,
                    "home_team": m.home_team,
                    "away_team": m.away_team,
                    "league": m.league,
                    "start_time": m.start_time,
                    "round_info": m.round_info,
                }
                for m in matches
            ]
            inserted = await upsert_upcoming_matches(match_dicts, scan_date)
            logger.info(f"Saved {inserted} new upcoming matches to DB")

            return {"ok": True, "count": len(matches), "saved": inserted}

        except Exception as e:
            logger.error(f"Upcoming refresh error: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}
