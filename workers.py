"""
Background workers:
  1. Anomaly scanner — runs every SCAN_INTERVAL_SECONDS
  2. Finished-match scanner — grades completed signal matches

The rolling next-24-hour fixture list is refreshed only on an explicit
dashboard request. It is not scheduled, analyzed by AI, or sent to Telegram.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from scraper import scraper
from detector import detect_anomalies, condition_a_dominant_side
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
_last_finished_match_report: dict | None = None

# A result scan must never keep the manual trigger locked indefinitely.  This
# is deliberately longer than one normal result batch, while still giving the
# dashboard a predictable upper bound when Sofascore or DNS is unhealthy.
FINISHED_MATCH_SCAN_TIMEOUT_SECONDS = 180
FINISHED_MATCH_BATCH_SIZE = 50


async def _get_match_statistics_bounded(matches) -> list:
    """Fetch stats without filling the scraper semaphore with every match.

    Creating one task per live match used to leave result checks stuck behind
    hundreds of statistics requests.  A small worker pool keeps only the
    requests that can actually run in flight, so higher-priority result checks
    can enter the shared scraper queue promptly.
    """
    results = [None] * len(matches)
    next_index = 0

    async def worker():
        nonlocal next_index
        while next_index < len(matches):
            index = next_index
            next_index += 1
            try:
                results[index] = await scraper.get_match_statistics(
                    matches[index].event_id
                )
            except Exception as exc:
                results[index] = exc

    worker_count = min(
        len(matches),
        max(1, getattr(scraper, "MAX_CONCURRENT_REQUESTS", 2)),
    )
    await asyncio.gather(*(worker() for _ in range(worker_count)))
    return results


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

            # Keep the number of queued requests bounded.  The scraper still
            # applies its own rate limit, but it no longer has hundreds of
            # live-stat requests waiting ahead of final-result checks.
            stats_results = await _get_match_statistics_bounded(eligible)

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
                    if condition_type == "A":
                        stats_dict["signal_side"] = condition_a_dominant_side(match, stats_result)
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
    global _last_finished_match_report

    if _finished_match_lock.locked():
        logger.info("Finished-match scan already running; waiting for its result")
        async with _finished_match_lock:
            if _last_finished_match_report is not None:
                return dict(_last_finished_match_report)
        # The previous owner was cancelled before publishing a report.  Retry
        # normally instead of surfacing a misleading permanent busy state.
        return await finished_match_scan()

    async with _finished_match_lock:
        try:
            event_ids = await get_pending_anomaly_match_ids(limit=FINISHED_MATCH_BATCH_SIZE)
            if not event_ids:
                logger.debug("No pending anomaly matches to finalize")
                report = {
                    "ok": True,
                    "checked": 0,
                    "matches_finished": 0,
                    "archived": 0,
                }
                _last_finished_match_report = report
                return dict(report)

            logger.info("Checking %s anomaly matches for final results", len(event_ids))
            tasks = {
                asyncio.create_task(scraper.get_match_result(event_id)): event_id
                for event_id in event_ids
            }
            pending = set(tasks)
            deadline = asyncio.get_running_loop().time() + FINISHED_MATCH_SCAN_TIMEOUT_SECONDS
            archived = matches_finished = errors = 0
            try:
                while pending:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    done, pending = await asyncio.wait(
                        pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        event_id = tasks[task]
                        if task.cancelled():
                            errors += 1
                            continue
                        try:
                            result = task.result()
                            if (
                                result is None or not result.is_finished
                                or result.score_home is None or result.score_away is None
                            ):
                                continue
                            # Commit each completed result immediately, even if
                            # another request later times out or is cancelled.
                            archived += await finalize_match_anomalies(
                                event_id, result.score_home, result.score_away
                            )
                            matches_finished += 1
                        except Exception as exc:
                            errors += 1
                            logger.warning("Result check failed for event %s: %s", event_id, exc)
                errors += len(pending)
                if pending:
                    logger.warning("Finished-match timeout: %s checks deferred", len(pending))
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            logger.info(
                "Finished-match scan complete: checked=%s finished=%s archived_signals=%s errors=%s",
                len(event_ids), matches_finished, archived, errors,
            )
            report = {
                "ok": True,
                "checked": len(event_ids),
                "matches_finished": matches_finished,
                "archived": archived,
                "errors": errors,
            }
            _last_finished_match_report = report
            return dict(report)
        except Exception as exc:
            logger.error("Finished-match scan error: %s", exc, exc_info=True)
            report = {
                "ok": False,
                "error": str(exc),
                "checked": 0,
                "archived": 0,
            }
            _last_finished_match_report = report
            return dict(report)


async def refresh_upcoming_matches() -> dict:
    """Fetch and store fixtures starting within the rolling next 24 hours."""
    if _upcoming_lock.locked():
        logger.debug("Upcoming refresh already running, skipping")
        return {"ok": False, "busy": True, "error": "Upcoming refresh already running"}

    async with _upcoming_lock:
        logger.info("Starting manual next-24-hour fixture refresh...")
        try:
            matches = await scraper.get_upcoming_matches()
            logger.info("Found %s fixtures in the next 24 hours", len(matches))

            if not matches:
                fetch_error = scraper.last_fetch_error or {}
                return {
                    "ok": not bool(fetch_error),
                    "count": 0,
                    "saved": 0,
                    "error": fetch_error.get("message"),
                }

            scan_date = datetime.now(TZ_TURKEY).strftime("%Y-%m-%d")
            match_dicts = [
                {
                    "event_id": match.event_id,
                    "home_team": match.home_team,
                    "away_team": match.away_team,
                    "league": match.league,
                    "start_time": match.start_time,
                    "round_info": match.round_info,
                }
                for match in matches
            ]
            saved = await upsert_upcoming_matches(match_dicts, scan_date)
            return {"ok": True, "count": len(matches), "saved": saved}
        except Exception as exc:
            logger.error("Upcoming refresh error: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc)}
