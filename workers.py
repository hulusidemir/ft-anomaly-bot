"""
Background workers:
  1. Anomaly scanner — runs every SCAN_INTERVAL_SECONDS
  2. Finished-match scanner — grades completed signal matches

The rolling next-24-hour fixture list is refreshed only on an explicit
dashboard request. It is not scheduled, analyzed by AI, or sent to Telegram.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

from scraper import scraper
from detector import RULE_VERSION, detect_anomalies_detailed
from config import TZ_TURKEY
from notifier import send_telegram, format_anomaly_message
from db import (
    insert_anomaly, mark_notified,
    upsert_upcoming_matches, mark_upcoming_anomaly,
    get_pending_anomaly_match_ids, finalize_match_anomalies,
    store_match_observation,
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


async def _refresh_live_match(match):
    """Return the current event state before making a signal decision."""
    current_matches = await scraper.get_live_matches()
    return next(
        (current for current in current_matches if current.event_id == match.event_id),
        None,
    )


async def _process_live_match(match):
    """Process one match independently once its statistics are available."""
    try:
        stats = await scraper.get_match_statistics(match.event_id)
    except Exception as exc:
        logger.debug("Stats request failed for %s: %s", match.event_id, exc)
        return 0, None
    if stats is None:
        return 0, None

    current = await _refresh_live_match(match)
    if current is None:
        return 0, None

    observed_at = time.time()
    stats_dict = stats.to_dict()
    if stats.validation_status == "INVALID" or current.minute is None:
        await store_match_observation(
            event_id=current.event_id,
            observed_at=observed_at,
            minute=current.minute,
            period=getattr(stats, "period", None),
            score_home=current.score_home,
            score_away=current.score_away,
            match_status=current.status_desc,
            league=current.league,
            home_team=current.home_team,
            away_team=current.away_team,
            normalized_stats=stats_dict,
            missing_fields=stats.missing_fields,
            validation_status=stats.validation_status,
            validation_errors=stats.validation_errors,
            event_fetched_at=observed_at,
            stats_fetched_at=stats.fetched_at,
            decision_at=observed_at,
            rule_version=RULE_VERSION,
        )
        return 0, None

    signals = detect_anomalies_detailed(current, stats)
    first_signal = signals[0] if signals else None
    observation_id = await store_match_observation(
        event_id=current.event_id,
        observed_at=observed_at,
        minute=current.minute,
        period=getattr(stats, "period", None),
        score_home=current.score_home,
        score_away=current.score_away,
        match_status=current.status_desc,
        league=current.league,
        home_team=current.home_team,
        away_team=current.away_team,
        normalized_stats=stats_dict,
        missing_fields=stats.missing_fields,
        validation_status=stats.validation_status,
        validation_errors=stats.validation_errors,
        provider="sofascore",
        event_fetched_at=observed_at,
        stats_fetched_at=stats.fetched_at,
        decision_at=observed_at,
        rule_version=RULE_VERSION,
        decision_outcome="anomaly" if first_signal else "no_signal",
        decision_condition=first_signal.condition if first_signal else None,
        selected_side=first_signal.side if first_signal else None,
        triggered_groups=first_signal.groups if first_signal else None,
        decision_reasons=first_signal.reasons if first_signal else None,
    )
    if not signals:
        return 0, None

    anomaly_count = 0
    for signal in signals:
        signal_stats = dict(stats_dict)
        signal_stats["signal_side"] = signal.side
        row_id, is_new, alert_number = await insert_anomaly(
            match_id=current.event_id,
            home_team=current.home_team,
            away_team=current.away_team,
            score_home=current.score_home,
            score_away=current.score_away,
            minute=current.minute,
            league=current.league,
            condition_type=signal.condition,
            triggered_rules=signal.reasons,
            stats_snapshot=signal_stats,
            selected_side=signal.side,
            triggered_groups=signal.groups,
            missing_fields=stats.missing_fields,
            stats_period=stats.period,
            event_fetched_at=observed_at,
            stats_fetched_at=stats.fetched_at,
            decision_at=observed_at,
            rule_version=RULE_VERSION,
            observation_id=observation_id,
        )

        if row_id and is_new:
            anomaly_count += 1
            msg = format_anomaly_message(
                home_team=current.home_team,
                away_team=current.away_team,
                score_home=current.score_home,
                score_away=current.score_away,
                minute=current.minute,
                league=current.league,
                condition_type=signal.condition,
                triggered_rules=signal.reasons,
                stats=signal_stats,
                alert_number=alert_number,
            )
            sent = await send_telegram(msg)
            if sent is not None:
                await mark_notified(row_id)

    return anomaly_count, first_signal


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
                minutes = [m.minute for m in matches if m.minute is not None]
                logger.info(
                    f"Minute range: {min(minutes)}-{max(minutes)}, "
                    f"distribution: {sorted(set(minutes))[:10]}"
                )

            # Filter: 30-85 min window.
            # Lower bound 30: stats are too sparse before ~30' for reliable
            #   ratio-based rules (cold starts, tactical probing).
            # Upper bound 85: catch late drama that 80 missed, but clip stoppage
            #   noise (90+) that rarely has room for follow-through.
            eligible = [m for m in matches if m.minute is not None and 30 <= m.minute <= 85]
            logger.info(f"Eligible matches (30-85 min): {len(eligible)}")

            if not eligible:
                return

            # Each task evaluates its match as soon as its own stats arrive.
            # A slow statistics request therefore cannot hold other matches.
            results = await asyncio.gather(
                *(_process_live_match(match) for match in eligible),
                return_exceptions=True,
            )
            anomaly_count = 0
            anomaly_event_ids: set[str] = set()
            for match, result in zip(eligible, results):
                if isinstance(result, Exception):
                    logger.warning("Live match processing failed for %s: %s", match.event_id, result)
                    continue
                count, signal = result
                anomaly_count += count
                if signal is not None:
                    anomaly_event_ids.add(match.event_id)

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
