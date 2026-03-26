"""
Background workers:
  1. Live match scanner — runs every SCAN_INTERVAL_SECONDS
  2. Upcoming match analyzer — runs twice daily at 07:00 and 19:00 Turkey time
"""

import asyncio
import logging
from datetime import datetime

from scraper import scraper, LiveMatch
from detector import detect_anomalies
from notifier import (
    send_telegram, format_anomaly_message,
    ask_gemini, build_gemini_prompt,
)
from db import insert_anomaly, mark_notified, insert_analysis

logger = logging.getLogger(__name__)

_scan_lock = asyncio.Lock()
_upcoming_lock = asyncio.Lock()


async def live_scan():
    """Worker 1: Scan live matches for anomalies."""
    if _scan_lock.locked():
        logger.debug("Live scan already running, skipping")
        return

    async with _scan_lock:
        logger.info("Starting live match scan...")
        try:
            matches = await scraper.get_live_matches()
            logger.info(f"Found {len(matches)} live matches")

            # Filter: only 20-80 minutes
            eligible = [m for m in matches if 20 <= m.minute <= 80]
            logger.info(f"Eligible matches (20-80 min): {len(eligible)}")

            if not eligible:
                return

            # Fetch stats concurrently (semaphore in scraper handles rate limiting)
            stats_tasks = [
                scraper.get_match_statistics(m.event_id) for m in eligible
            ]
            stats_results = await asyncio.gather(*stats_tasks, return_exceptions=True)

            anomaly_count = 0
            for match, stats_result in zip(eligible, stats_results):
                if isinstance(stats_result, Exception) or stats_result is None:
                    continue

                anomalies = detect_anomalies(match, stats_result)
                for condition_type, rules in anomalies:
                    stats_dict = stats_result.to_dict()
                    row_id = await insert_anomaly(
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

                    if row_id:
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
                        )
                        sent = await send_telegram(msg)
                        if sent:
                            await mark_notified(row_id)

            if anomaly_count > 0:
                logger.info(f"Detected {anomaly_count} new anomalies")
            else:
                logger.debug("No new anomalies found")

        except Exception as e:
            logger.error(f"Live scan error: {e}", exc_info=True)


async def upcoming_scan(run_type: str = "morning"):
    """Worker 2: Fetch upcoming matches and get Gemini analysis."""
    if _upcoming_lock.locked():
        logger.debug("Upcoming scan already running, skipping")
        return

    async with _upcoming_lock:
        logger.info(f"Starting upcoming match analysis ({run_type})...")
        try:
            matches = await scraper.get_upcoming_matches()
            logger.info(f"Found {len(matches)} upcoming matches")

            if not matches:
                await send_telegram("📋 Bugün için yaklaşan maç bulunamadı.")
                return

            # Build match list text for Gemini
            match_lines = []
            for m in matches:
                line = f"• {m.league}: {m.home_team} - {m.away_team} (Başlangıç: {m.start_time} UTC)"
                if m.round_info:
                    line += f" [{m.round_info}]"
                match_lines.append(line)

            matches_text = "\n".join(match_lines)
            prompt = build_gemini_prompt(matches_text)

            # Ask Gemini for analysis
            analysis = await ask_gemini(prompt)

            if not analysis:
                logger.error("Gemini returned no analysis")
                await send_telegram("⚠️ Yaklaşan maçlar için Gemini analizi alınamadı.")
                return

            # Save to database
            await insert_analysis(
                text=analysis,
                match_count=len(matches),
                run_type=run_type,
            )

            # Send to Telegram
            header = (
                f"🔮 <b>Maç Analizi — "
                f"{'Sabah' if run_type == 'morning' else 'Akşam'} Raporu</b>\n"
                f"📅 {datetime.utcnow().strftime('%Y-%m-%d')}\n"
                f"📊 {len(matches)} maç analiz edildi\n\n"
            )
            await send_telegram(header + analysis)
            logger.info("Upcoming analysis sent successfully")

        except Exception as e:
            logger.error(f"Upcoming scan error: {e}", exc_info=True)
