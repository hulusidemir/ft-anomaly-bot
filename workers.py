"""
Background workers:
  1. Live match scanner — runs every SCAN_INTERVAL_SECONDS
  2. Upcoming match analyzer — runs twice daily at 07:00 and 19:00 Turkey time
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from scraper import scraper, UpcomingMatch
from collections import defaultdict
from detector import detect_anomalies
from config import TZ_TURKEY
from notifier import (
    send_telegram, format_anomaly_message,
    ask_gemini, build_gemini_prompt,
)
from db import (
    insert_anomaly, mark_notified, insert_analysis,
    upsert_upcoming_matches, mark_upcoming_anomaly,
)

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

            if matches:
                minutes = [m.minute for m in matches]
                logger.info(
                    f"Minute range: {min(minutes)}-{max(minutes)}, "
                    f"distribution: {sorted(set(minutes))[:10]}"
                )

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

            stats_ok = sum(1 for s in stats_results if s is not None and not isinstance(s, Exception))
            logger.info(f"Stats fetched: {stats_ok}/{len(eligible)} successful")

            anomaly_count = 0
            for match, stats_result in zip(eligible, stats_results):
                if isinstance(stats_result, Exception) or stats_result is None:
                    logger.debug(
                        f"No stats for {match.home_team} vs {match.away_team} "
                        f"(id={match.event_id})"
                    )
                    continue

                anomalies = detect_anomalies(match, stats_result)
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
                # Mark these matches in the upcoming_matches table
                anomaly_event_ids = list({
                    m.event_id for m, s in zip(eligible, stats_results)
                    if s is not None and not isinstance(s, Exception)
                    and detect_anomalies(m, s)
                })
                if anomaly_event_ids:
                    scan_date = datetime.now(TZ_TURKEY).strftime("%Y-%m-%d")
                    await mark_upcoming_anomaly(anomaly_event_ids, scan_date)
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

            # ── 1. Send formatted match list to Telegram ──
            match_list_msg = _format_match_list(matches, run_type)
            list_msg_id = await send_telegram(match_list_msg)

            if not list_msg_id:
                logger.error("Failed to send match list to Telegram")

            # ── 2. Build prompt & ask Gemini ──
            match_lines = []
            for m in matches:
                t = _fmt_time_utc3(m.start_time)
                line = f"• {m.league}: {m.home_team} - {m.away_team} (Başlangıç: {t})"
                if m.round_info:
                    line += f" [{m.round_info}]"
                match_lines.append(line)

            matches_text = "\n".join(match_lines)
            prompt = build_gemini_prompt(matches_text)

            analysis = await ask_gemini(prompt)

            if not analysis:
                logger.error("Gemini returned no analysis")
                await send_telegram(
                    "⚠️ Yaklaşan maçlar için Gemini analizi alınamadı.",
                    reply_to_message_id=list_msg_id,
                )
                return

            # Save to database
            await insert_analysis(
                text=analysis,
                match_count=len(matches),
                run_type=run_type,
            )

            # ── 3. Send Gemini analysis as reply to match list ──
            header = (
                f"🔮 <b>Maç Analizi — "
                f"{'Sabah' if run_type == 'morning' else 'Akşam'} Raporu</b>\n"
                f"📅 {datetime.now(TZ_TURKEY).strftime('%Y-%m-%d')}\n"
                f"📊 {len(matches)} maç analiz edildi\n\n"
            )
            await send_telegram(
                header + analysis,
                reply_to_message_id=list_msg_id,
            )
            logger.info("Upcoming analysis sent successfully")

        except Exception as e:
            logger.error(f"Upcoming scan error: {e}", exc_info=True)


def _fmt_time_utc3(ts_str: str) -> str:
    """Convert start_time (Unix ts string) to HH:MM UTC+3."""
    try:
        ts = int(ts_str)
        if ts > 0:
            return datetime.fromtimestamp(ts, tz=TZ_TURKEY).strftime("%H:%M")
    except (ValueError, OSError):
        pass
    return "TBD"


def _format_match_list(matches: list[UpcomingMatch], run_type: str) -> str:
    """Format upcoming matches grouped by country/league for Telegram."""
    today = datetime.now(TZ_TURKEY).strftime("%Y-%m-%d")
    period = "Sabah" if run_type == "morning" else "Akşam"

    lines = [
        f"⚽ <b>Günün Maç Programı — {period}</b>",
        f"📅 {today}",
        f"📊 Toplam <b>{len(matches)}</b> maç\n",
    ]

    # Group by league (league already contains "Country - League")
    by_league: dict[str, list[UpcomingMatch]] = defaultdict(list)
    for m in matches:
        by_league[m.league].append(m)

    # Sort leagues alphabetically
    for league in sorted(by_league):
        league_matches = by_league[league]
        lines.append(f"🏆 <b>{league}</b>")
        for m in sorted(league_matches, key=lambda x: x.start_time):
            line = f"  ⏰ {_fmt_time_utc3(m.start_time)} — {m.home_team} vs {m.away_team}"
            if m.round_info:
                line += f"  <i>({m.round_info})</i>"
            lines.append(line)
        lines.append("")

    return "\n".join(lines)
