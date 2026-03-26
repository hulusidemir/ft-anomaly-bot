import asyncio
import random
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from curl_cffi.requests import AsyncSession
from config import SOFASCORE_BASE

logger = logging.getLogger(__name__)

# Impersonation targets for curl_cffi (rotated on 403)
IMPERSONATE_TARGETS = ["chrome", "chrome110", "chrome120"]


@dataclass
class MatchStats:
    possession_home: float = 0.0
    possession_away: float = 0.0
    dangerous_attacks_home: int = 0
    dangerous_attacks_away: int = 0
    total_shots_home: int = 0
    total_shots_away: int = 0
    shots_on_target_home: int = 0
    shots_on_target_away: int = 0
    yellow_cards_home: int = 0
    yellow_cards_away: int = 0
    red_cards_home: int = 0
    red_cards_away: int = 0

    def to_dict(self) -> dict:
        return {
            "possession_home": self.possession_home,
            "possession_away": self.possession_away,
            "dangerous_attacks_home": self.dangerous_attacks_home,
            "dangerous_attacks_away": self.dangerous_attacks_away,
            "total_shots_home": self.total_shots_home,
            "total_shots_away": self.total_shots_away,
            "shots_on_target_home": self.shots_on_target_home,
            "shots_on_target_away": self.shots_on_target_away,
            "yellow_cards_home": self.yellow_cards_home,
            "yellow_cards_away": self.yellow_cards_away,
            "red_cards_home": self.red_cards_home,
            "red_cards_away": self.red_cards_away,
        }


@dataclass
class LiveMatch:
    event_id: str
    home_team: str
    away_team: str
    score_home: int
    score_away: int
    minute: int
    league: str
    status_desc: str
    stats: MatchStats | None = None


@dataclass
class UpcomingMatch:
    event_id: str
    home_team: str
    away_team: str
    league: str
    start_time: str
    round_info: str = ""


class SofascoreScraper:
    def __init__(self, max_concurrent: int = 4):
        self._session: AsyncSession | None = None
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._last_request_time = 0.0
        self._impersonate = "chrome"

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(
                impersonate=self._impersonate,
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.sofascore.com/",
                    "Cache-Control": "no-cache",
                },
                timeout=15,
            )
        return self._session

    async def _fetch_json(self, url: str, retries: int = 3) -> dict | None:
        async with self._semaphore:
            # Enforce minimum delay between requests
            elapsed = time.monotonic() - self._last_request_time
            min_delay = random.uniform(1.2, 2.8)
            if elapsed < min_delay:
                await asyncio.sleep(min_delay - elapsed)

            session = await self._get_session()
            for attempt in range(retries):
                try:
                    self._last_request_time = time.monotonic()
                    resp = await session.get(url)
                    if resp.status_code == 200:
                        return resp.json()
                    elif resp.status_code == 429:
                        wait = (2 ** attempt) * 5 + random.uniform(1, 3)
                        logger.warning(f"Rate limited (429) on {url}, waiting {wait:.1f}s")
                        await asyncio.sleep(wait)
                    elif resp.status_code == 403:
                        wait = (2 ** attempt) * 3 + random.uniform(1, 2)
                        logger.warning(f"Forbidden (403) on {url}, attempt {attempt+1}")
                        # Rotate impersonation target
                        await self._rotate_session()
                        session = await self._get_session()
                        await asyncio.sleep(wait)
                    else:
                        logger.warning(f"HTTP {resp.status_code} on {url}")
                        return None
                except Exception as e:
                    logger.warning(f"Request error on {url}: {e}")
                    await asyncio.sleep(2 * (attempt + 1))
            return None

    async def _rotate_session(self):
        """Close current session and create a new one with different impersonation."""
        if self._session:
            await self._session.close()
            self._session = None
        self._impersonate = random.choice(IMPERSONATE_TARGETS)

    def _parse_minute(self, event: dict) -> int:
        """Extract current match minute from event data."""
        # Try statusTime.played first (seconds)
        status_time = event.get("statusTime", {})
        if status_time and status_time.get("played"):
            return status_time["played"] // 60

        # Try time.currentPeriodStart
        time_data = event.get("time", {})
        if time_data:
            period_start = time_data.get("currentPeriodStartTimestamp")
            if period_start:
                now = int(datetime.utcnow().timestamp())
                elapsed = (now - period_start) // 60
                # Second half adds 45 minutes
                status = event.get("status", {})
                code = status.get("code", 0)
                if code == 7:  # 2nd half
                    elapsed += 45
                return max(0, min(elapsed, 120))

            # Try time.played directly
            played = time_data.get("played")
            if played is not None:
                return played // 60 if played > 90 else played

        # Try status description fallback (e.g. "45+2")
        status = event.get("status", {})
        desc = status.get("description", "")
        if desc and desc[0].isdigit():
            try:
                return int(desc.split("+")[0].split("'")[0])
            except (ValueError, IndexError):
                pass

        # Fallback: check lastPeriod start
        changes = event.get("changes", {})
        if changes:
            change_ts = changes.get("changeTimestamp")
            if change_ts:
                elapsed_s = int(datetime.utcnow().timestamp()) - change_ts
                return max(0, elapsed_s // 60)

        return 0

    async def get_live_matches(self) -> list[LiveMatch]:
        """Fetch all currently live football matches."""
        data = await self._fetch_json(f"{SOFASCORE_BASE}/sport/football/events/live")
        if not data:
            logger.error("Failed to fetch live matches")
            return []

        matches = []
        for event in data.get("events", []):
            try:
                status = event.get("status", {})
                status_type = status.get("type", "")
                # Only include in-progress matches
                if status_type != "inprogress":
                    continue

                minute = self._parse_minute(event)

                home = event.get("homeTeam", {})
                away = event.get("awayTeam", {})
                home_score_data = event.get("homeScore", {})
                away_score_data = event.get("awayScore", {})

                tournament = event.get("tournament", {})
                category = tournament.get("category", {})
                league_name = tournament.get("name", "Unknown")
                country = category.get("name", "")
                full_league = f"{country} - {league_name}" if country else league_name

                match = LiveMatch(
                    event_id=str(event.get("id", "")),
                    home_team=home.get("name", "Unknown"),
                    away_team=away.get("name", "Unknown"),
                    score_home=home_score_data.get("current", 0) or 0,
                    score_away=away_score_data.get("current", 0) or 0,
                    minute=minute,
                    league=full_league,
                    status_desc=status.get("description", ""),
                )
                matches.append(match)
            except Exception as e:
                logger.debug(f"Error parsing event: {e}")
                continue

        return matches

    def _parse_stat_value(self, value: str) -> float:
        """Parse stat values like '58%', '12', etc."""
        if not value:
            return 0.0
        value = str(value).strip().replace("%", "")
        try:
            return float(value)
        except ValueError:
            return 0.0

    async def get_match_statistics(self, event_id: str) -> MatchStats | None:
        """Fetch detailed statistics for a specific match."""
        data = await self._fetch_json(f"{SOFASCORE_BASE}/event/{event_id}/statistics")
        if not data:
            return None

        stats = MatchStats()
        stat_periods = data.get("statistics", [])

        # Look for "ALL" period, fallback to last available
        target_period = None
        for period in stat_periods:
            if period.get("period") == "ALL":
                target_period = period
                break
        if not target_period and stat_periods:
            target_period = stat_periods[-1]
        if not target_period:
            return None

        shots_on_home = 0
        shots_on_away = 0
        shots_off_home = 0
        shots_off_away = 0
        blocked_home = 0
        blocked_away = 0

        for group in target_period.get("groups", []):
            for item in group.get("statisticsItems", []):
                key = item.get("key", "").lower()
                name = item.get("name", "").lower()
                home_val = item.get("home", "0")
                away_val = item.get("away", "0")

                if key == "ballpossession" or "ball possession" in name or name == "possession":
                    stats.possession_home = self._parse_stat_value(home_val)
                    stats.possession_away = self._parse_stat_value(away_val)
                elif key == "dangerousattacks" or "dangerous attack" in name:
                    stats.dangerous_attacks_home = int(self._parse_stat_value(home_val))
                    stats.dangerous_attacks_away = int(self._parse_stat_value(away_val))
                elif key == "shotsongoal" or "shots on target" in name or name == "on target":
                    val_h = int(self._parse_stat_value(home_val))
                    val_a = int(self._parse_stat_value(away_val))
                    stats.shots_on_target_home = val_h
                    stats.shots_on_target_away = val_a
                    shots_on_home = val_h
                    shots_on_away = val_a
                elif key == "shotsoffgoal" or "shots off target" in name or name == "off target":
                    shots_off_home = int(self._parse_stat_value(home_val))
                    shots_off_away = int(self._parse_stat_value(away_val))
                elif key == "blockedscoringattempt" or "blocked shot" in name:
                    blocked_home = int(self._parse_stat_value(home_val))
                    blocked_away = int(self._parse_stat_value(away_val))
                elif key == "totalshots" or name in ("total shots", "shots total", "total attempts"):
                    stats.total_shots_home = int(self._parse_stat_value(home_val))
                    stats.total_shots_away = int(self._parse_stat_value(away_val))
                elif key == "yellowcards" or "yellow card" in name:
                    stats.yellow_cards_home = int(self._parse_stat_value(home_val))
                    stats.yellow_cards_away = int(self._parse_stat_value(away_val))
                elif key == "redcards" or "red card" in name:
                    stats.red_cards_home = int(self._parse_stat_value(home_val))
                    stats.red_cards_away = int(self._parse_stat_value(away_val))

        # Calculate total shots if not directly provided
        if stats.total_shots_home == 0 and (shots_on_home + shots_off_home + blocked_home) > 0:
            stats.total_shots_home = shots_on_home + shots_off_home + blocked_home
        if stats.total_shots_away == 0 and (shots_on_away + shots_off_away + blocked_away) > 0:
            stats.total_shots_away = shots_on_away + shots_off_away + blocked_away

        return stats

    async def get_upcoming_matches(self) -> list[UpcomingMatch]:
        """Fetch today's upcoming (scheduled) football matches."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        data = await self._fetch_json(
            f"{SOFASCORE_BASE}/sport/football/scheduled-events/{today}"
        )
        if not data:
            logger.error("Failed to fetch upcoming matches")
            return []

        matches = []
        for event in data.get("events", []):
            try:
                status = event.get("status", {})
                status_type = status.get("type", "")
                # Only include not-started matches
                if status_type != "notstarted":
                    continue

                home = event.get("homeTeam", {})
                away = event.get("awayTeam", {})
                tournament = event.get("tournament", {})
                category = tournament.get("category", {})

                league_name = tournament.get("name", "Unknown")
                country = category.get("name", "")
                full_league = f"{country} - {league_name}" if country else league_name

                start_ts = event.get("startTimestamp", 0)
                start_time = datetime.utcfromtimestamp(start_ts).strftime("%H:%M") if start_ts else "TBD"

                round_info_data = event.get("roundInfo", {})
                round_str = ""
                if round_info_data:
                    round_str = f"Round {round_info_data.get('round', '')}"

                matches.append(UpcomingMatch(
                    event_id=str(event.get("id", "")),
                    home_team=home.get("name", "Unknown"),
                    away_team=away.get("name", "Unknown"),
                    league=full_league,
                    start_time=start_time,
                    round_info=round_str,
                ))
            except Exception as e:
                logger.debug(f"Error parsing upcoming event: {e}")
                continue

        return matches

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None


# Singleton scraper instance
scraper = SofascoreScraper(max_concurrent=4)
