import asyncio
import random
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

from curl_cffi.requests import AsyncSession
from config import SOFASCORE_BASE, TZ_TURKEY

logger = logging.getLogger(__name__)

# Modern browser impersonation targets (rotated on bot-protection hits)
IMPERSONATE_TARGETS = [
    "chrome131", "chrome124", "chrome123", "chrome120",
    "safari17_0", "safari17_2_ios", "edge101",
]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,tr;q=0.8",
    "tr-TR,tr;q=0.9,en;q=0.8",
    "en-GB,en;q=0.9,tr;q=0.7",
]

SOFASCORE_WEB = "https://www.sofascore.com"
# Endpoints that should never legitimately 404 – treat 404 on these as bot-protection masking.
LIST_ENDPOINTS = ("/events/live", "/scheduled-events/")


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
    shots_off_target_home: int = 0
    shots_off_target_away: int = 0
    blocked_shots_home: int = 0
    blocked_shots_away: int = 0
    big_chances_home: int = 0
    big_chances_away: int = 0
    expected_goals_home: float = 0.0
    expected_goals_away: float = 0.0
    yellow_cards_home: int = 0
    yellow_cards_away: int = 0
    red_cards_home: int = 0
    red_cards_away: int = 0
    offsides_home: int = 0
    offsides_away: int = 0
    corner_kicks_home: int = 0
    corner_kicks_away: int = 0
    fouls_home: int = 0
    fouls_away: int = 0
    accurate_passes_home: int = 0
    accurate_passes_away: int = 0
    total_passes_home: int = 0
    total_passes_away: int = 0
    pass_accuracy_home: float = 0.0
    pass_accuracy_away: float = 0.0

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
            "shots_off_target_home": self.shots_off_target_home,
            "shots_off_target_away": self.shots_off_target_away,
            "blocked_shots_home": self.blocked_shots_home,
            "blocked_shots_away": self.blocked_shots_away,
            "big_chances_home": self.big_chances_home,
            "big_chances_away": self.big_chances_away,
            "expected_goals_home": self.expected_goals_home,
            "expected_goals_away": self.expected_goals_away,
            "yellow_cards_home": self.yellow_cards_home,
            "yellow_cards_away": self.yellow_cards_away,
            "red_cards_home": self.red_cards_home,
            "red_cards_away": self.red_cards_away,
            "offsides_home": self.offsides_home,
            "offsides_away": self.offsides_away,
            "corner_kicks_home": self.corner_kicks_home,
            "corner_kicks_away": self.corner_kicks_away,
            "fouls_home": self.fouls_home,
            "fouls_away": self.fouls_away,
            "accurate_passes_home": self.accurate_passes_home,
            "accurate_passes_away": self.accurate_passes_away,
            "total_passes_home": self.total_passes_home,
            "total_passes_away": self.total_passes_away,
            "pass_accuracy_home": self.pass_accuracy_home,
            "pass_accuracy_away": self.pass_accuracy_away,
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
    # Concurrency: keep it low – a real browser never fires dozens of parallel XHRs.
    MAX_CONCURRENT_REQUESTS = 2
    # Jittered gap between any two requests (seconds).
    MIN_GAP_RANGE = (1.8, 3.6)
    # Re-warm session if older than this (seconds).
    WARMUP_TTL = 900

    def __init__(self):
        self._session: AsyncSession | None = None
        self._rate_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_REQUESTS)
        self._last_request_time = 0.0
        self._last_rotate_time = 0.0
        self._impersonate = random.choice(IMPERSONATE_TARGETS)
        self._accept_lang = random.choice(ACCEPT_LANGUAGES)
        self._session_warm_at = 0.0
        self._consecutive_bot_errors = 0
        self.last_fetch_error: dict | None = None
        self.last_live_fetch_error: dict | None = None

    def _build_session(self) -> AsyncSession:
        # NOTE: do NOT set Sec-Fetch-* / Sec-Ch-Ua-* manually – curl_cffi's
        # impersonate profile sets them together with the matching TLS/JA3
        # fingerprint. Overriding piecemeal creates inconsistency that the
        # WAF detects. We only set app-level headers here.
        return AsyncSession(
            impersonate=self._impersonate,
            headers={
                "Accept": "*/*",
                "Accept-Language": self._accept_lang,
                "Referer": f"{SOFASCORE_WEB}/",
                "Origin": SOFASCORE_WEB,
            },
            timeout=20,
        )

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = self._build_session()
            self._session_warm_at = 0.0
        return self._session

    async def _warm_session(self) -> None:
        """Visit the Sofascore homepage once to bootstrap Cloudflare cookies.

        Without this, the very first API call on a fresh session has no
        cf_clearance/__cf_bm cookie and is far more likely to be challenged.
        """
        now = time.monotonic()
        if self._session_warm_at and (now - self._session_warm_at) < self.WARMUP_TTL:
            return
        try:
            sess = await self._get_session()
            resp = await sess.get(SOFASCORE_WEB, timeout=15)
            if resp.status_code in (200, 304):
                self._session_warm_at = time.monotonic()
                # Small human-like pause before issuing the first XHR
                await asyncio.sleep(random.uniform(0.8, 2.0))
                logger.debug("Session warmed (impersonate=%s)", self._impersonate)
            else:
                logger.warning(f"Warm-up unexpected status {resp.status_code}")
        except Exception as e:
            logger.warning(f"Session warm-up failed: {e}")

    async def _throttle(self) -> None:
        async with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_time
            min_delay = random.uniform(*self.MIN_GAP_RANGE)
            if elapsed < min_delay:
                await asyncio.sleep(min_delay - elapsed)
            self._last_request_time = time.monotonic()

    async def _rotate_session(self) -> None:
        """Drop the current session, pick a different impersonation, and force re-warm.

        Kept behind a 5s cool-down so a burst of errors doesn't rotate many
        times in a row (which itself looks bot-like and depletes our IP budget).
        """
        elapsed = time.monotonic() - self._last_rotate_time
        if elapsed < 5:
            await asyncio.sleep(5 - elapsed)
        self._last_rotate_time = time.monotonic()

        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

        others = [t for t in IMPERSONATE_TARGETS if t != self._impersonate]
        if others:
            self._impersonate = random.choice(others)
        self._accept_lang = random.choice(ACCEPT_LANGUAGES)
        self._session_warm_at = 0.0
        self._consecutive_bot_errors = 0
        logger.info("Rotated scraper session – new impersonate=%s", self._impersonate)

    def _is_list_endpoint(self, url: str) -> bool:
        return any(marker in url for marker in LIST_ENDPOINTS)

    async def _fetch_json(self, url: str, retries: int = 5) -> dict | None:
        """Fetch JSON with bot-protection-aware retry logic.

        Handling strategy:
          * 200            → return JSON
          * 403 / 429      → always bot-protection: rotate + backoff
          * 404 on a list  → treat as bot-protection masking: rotate + retry
          * 404 on detail  → legit "no data" on first attempt; only rotate on
                             repeated bursts (tracked via _consecutive_bot_errors)
          * 5xx            → transient: backoff, no rotation
        """
        list_endpoint = self._is_list_endpoint(url)
        self.last_fetch_error = None

        for attempt in range(retries):
            await self._warm_session()
            async with self._semaphore:
                await self._throttle()
                try:
                    session = await self._get_session()
                    resp = await session.get(url)
                except Exception as e:
                    self.last_fetch_error = {
                        "url": url,
                        "status": None,
                        "message": f"Request error: {e}",
                    }
                    logger.warning(f"Request error on {url}: {e}")
                    await self._rotate_session()
                    await asyncio.sleep(2 * (attempt + 1) + random.uniform(0.5, 2.0))
                    continue

            status = resp.status_code

            if status == 200:
                self._consecutive_bot_errors = 0
                try:
                    self.last_fetch_error = None
                    return resp.json()
                except Exception:
                    self.last_fetch_error = {
                        "url": url,
                        "status": status,
                        "message": "Non-JSON response",
                    }
                    logger.warning(f"Non-JSON 200 from {url}")
                    return None

            if status == 429:
                self._consecutive_bot_errors += 1
                self.last_fetch_error = {
                    "url": url,
                    "status": status,
                    "message": "Rate limited by Sofascore",
                }
                wait = (2 ** attempt) * 5 + random.uniform(2, 6)
                logger.warning(f"Rate limited (429) on {url}, waiting {wait:.1f}s")
                await asyncio.sleep(wait)
                if attempt >= 1:
                    await self._rotate_session()
                continue

            if status == 403:
                self._consecutive_bot_errors += 1
                self.last_fetch_error = {
                    "url": url,
                    "status": status,
                    "message": "Forbidden by Sofascore",
                }
                wait = (2 ** attempt) * 3 + random.uniform(3, 7)
                logger.warning(f"Forbidden (403) on {url} – rotating session")
                await self._rotate_session()
                await asyncio.sleep(wait)
                continue

            if status == 404:
                # On list endpoints, 404 is never legit – always bot-protection masking.
                # On detail endpoints, a single 404 is usually legit (no stats yet).
                if list_endpoint:
                    self._consecutive_bot_errors += 1
                    self.last_fetch_error = {
                        "url": url,
                        "status": status,
                        "message": "List endpoint returned 404",
                    }
                    wait = (2 ** attempt) * 3 + random.uniform(2, 5)
                    logger.warning(
                        f"404 on list endpoint {url} – treating as bot-protection, "
                        f"rotating and retrying in {wait:.1f}s"
                    )
                    await self._rotate_session()
                    await asyncio.sleep(wait)
                    continue

                # Detail endpoint 404 – accept unless we're seeing a burst.
                if self._consecutive_bot_errors >= 3 and attempt == 0:
                    self.last_fetch_error = {
                        "url": url,
                        "status": status,
                        "message": "Detail endpoint returned 404 during 404 burst",
                    }
                    logger.warning(
                        f"404 on {url} during 404-burst (count={self._consecutive_bot_errors})"
                        f" – rotating before giving up"
                    )
                    await self._rotate_session()
                    await asyncio.sleep(random.uniform(2, 4))
                    self._consecutive_bot_errors = 0
                    continue
                self._consecutive_bot_errors += 1
                self.last_fetch_error = {
                    "url": url,
                    "status": status,
                    "message": "Detail endpoint returned 404",
                }
                logger.debug(f"404 on {url} (likely no data)")
                return None

            if status >= 500:
                self.last_fetch_error = {
                    "url": url,
                    "status": status,
                    "message": "Sofascore server error",
                }
                wait = (2 ** attempt) * 2 + random.uniform(1, 2)
                logger.warning(f"Server error ({status}) on {url}, retrying in {wait:.1f}s")
                await asyncio.sleep(wait)
                continue

            self.last_fetch_error = {
                "url": url,
                "status": status,
                "message": f"Unexpected HTTP {status}",
            }
            logger.warning(f"HTTP {status} on {url}")
            return None

        return None

    def _parse_minute(self, event: dict) -> int:
        """Extract current match minute from event data.

        SofaScore provides:
          statusTime.initial  – elapsed seconds at period start (0 for 1st half, 2700 for 2nd)
          statusTime.timestamp – UNIX timestamp when the current period clock started
        Formula: minute = (initial + (now - timestamp)) / 60
        """
        now = int(time.time())

        # Primary: use statusTime.initial + elapsed since timestamp
        status_time = event.get("statusTime", {})
        ts = status_time.get("timestamp")
        if ts and ts > 0:
            initial = status_time.get("initial", 0)
            elapsed = now - ts
            minute = (initial + max(elapsed, 0)) // 60
            return max(0, min(int(minute), 130))

        # Fallback: time.currentPeriodStartTimestamp
        time_data = event.get("time", {})
        if time_data:
            period_start = time_data.get("currentPeriodStartTimestamp")
            if period_start and period_start > 0:
                initial = time_data.get("initial", 0)
                elapsed = now - period_start
                minute = (initial + max(elapsed, 0)) // 60
                return max(0, min(int(minute), 130))

        # Fallback: status description (e.g. "1st half", "2nd half")
        status = event.get("status", {})
        desc = status.get("description", "")
        if desc and desc[0].isdigit():
            try:
                return int(desc.split("+")[0].split("'")[0])
            except (ValueError, IndexError):
                pass

        # Last resort: estimate from startTimestamp (accounts for HT break).
        start_ts = event.get("startTimestamp", 0)
        if start_ts and start_ts > 0:
            elapsed_min = (now - start_ts) // 60
            # Subtract half-time break (15 min) if we're past 45 mins elapsed.
            if elapsed_min > 45:
                elapsed_min -= 15
            return max(0, min(int(elapsed_min), 130))

        return 0

    async def get_live_matches(self, retries: int = 5) -> list[LiveMatch]:
        """Fetch all currently live football matches."""
        data = await self._fetch_json(
            f"{SOFASCORE_BASE}/sport/football/events/live",
            retries=retries,
        )
        if not data:
            self.last_live_fetch_error = self.last_fetch_error or {
                "url": f"{SOFASCORE_BASE}/sport/football/events/live",
                "status": None,
                "message": "No response from Sofascore",
            }
            logger.error("Failed to fetch live matches")
            return []
        self.last_live_fetch_error = None

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

    _STAT_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

    def _parse_stat_value(self, value) -> float:
        """Parse Sofascore stat values.

        Sofascore returns stats in multiple shapes:
          * pure numbers (int/float)
          * percent strings like "58%"
          * ratio strings like "330/420 (79%)" — we want the first number
          * "5 (2)" — first number
        """
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        if not s:
            return 0.0
        match = self._STAT_NUM_RE.search(s.replace(",", "."))
        if not match:
            return 0.0
        try:
            return float(match.group(0))
        except ValueError:
            return 0.0

    def _parse_percent(self, value) -> float:
        """Extract a percent value. If the string is 'x/y (z%)' we return z."""
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value)
        # Look for an embedded percent first; many Sofascore strings are
        # formatted as "330/420 (79%)" where the raw first number is the
        # accurate count, not the percentage.
        pct_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", s)
        if pct_match:
            try:
                return float(pct_match.group(1).replace(",", "."))
            except ValueError:
                return 0.0
        return self._parse_stat_value(s)

    def _parse_ratio(self, value) -> tuple[float, float]:
        """Parse a ratio like '330/420 (79%)' → (330, 420). Returns (num, 0) if not a ratio."""
        if value is None:
            return 0.0, 0.0
        s = str(value)
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)", s)
        if m:
            try:
                return (
                    float(m.group(1).replace(",", ".")),
                    float(m.group(2).replace(",", ".")),
                )
            except ValueError:
                return 0.0, 0.0
        # Fall back to a single number (accurate count only)
        return self._parse_stat_value(s), 0.0

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

        seen_total_shots = False

        for group in target_period.get("groups", []):
            for item in group.get("statisticsItems", []):
                key = item.get("key", "") or ""
                key_lc = key.lower()
                name_lc = (item.get("name", "") or "").lower()
                # homeValue / awayValue are numeric when available; home / away
                # are display strings (e.g. "5 (2)", "330/420 (79%)").
                home_raw = item.get("homeValue")
                away_raw = item.get("awayValue")
                home_display = item.get("home", "")
                away_display = item.get("away", "")
                home_val = home_raw if home_raw is not None else home_display
                away_val = away_raw if away_raw is not None else away_display

                key_norm = re.sub(r"[^a-z0-9]", "", key_lc)
                name_norm = re.sub(r"[^a-z0-9]", "", name_lc)

                def _match(*tokens) -> bool:
                    normalized = [re.sub(r"[^a-z0-9]", "", t.lower()) for t in tokens]
                    return (
                        key_norm in normalized
                        or name_norm in normalized
                        or any(t and (t in key_norm or t in name_norm) for t in normalized)
                    )

                if _match("ballpossession", "ball possession", "possession"):
                    stats.possession_home = self._parse_percent(home_display or home_val)
                    stats.possession_away = self._parse_percent(away_display or away_val)
                elif _match("expectedgoals", "expected goals", "xg"):
                    stats.expected_goals_home = self._parse_stat_value(home_val)
                    stats.expected_goals_away = self._parse_stat_value(away_val)
                elif (
                    _match("bigchancecreated", "big chances", "big chance")
                    and "missed" not in key_norm
                    and "missed" not in name_norm
                ):
                    stats.big_chances_home = int(self._parse_stat_value(home_val))
                    stats.big_chances_away = int(self._parse_stat_value(away_val))
                elif _match("dangerousattacks", "dangerous attacks", "dangerous attack"):
                    stats.dangerous_attacks_home = int(self._parse_stat_value(home_val))
                    stats.dangerous_attacks_away = int(self._parse_stat_value(away_val))
                elif _match("totalshotsongoal", "totalshots", "total shots", "shots total", "total attempts"):
                    stats.total_shots_home = int(self._parse_stat_value(home_val))
                    stats.total_shots_away = int(self._parse_stat_value(away_val))
                    seen_total_shots = True
                elif _match("shotsongoal", "shotsontarget", "shots on target", "on target"):
                    stats.shots_on_target_home = int(self._parse_stat_value(home_val))
                    stats.shots_on_target_away = int(self._parse_stat_value(away_val))
                elif _match("shotsoffgoal", "shotsofftarget", "shots off target", "off target"):
                    stats.shots_off_target_home = int(self._parse_stat_value(home_val))
                    stats.shots_off_target_away = int(self._parse_stat_value(away_val))
                elif _match("blockedscoringattempt", "blocked shots", "blocked shot"):
                    stats.blocked_shots_home = int(self._parse_stat_value(home_val))
                    stats.blocked_shots_away = int(self._parse_stat_value(away_val))
                elif _match("cornerkicks", "corners", "corner kicks"):
                    stats.corner_kicks_home = int(self._parse_stat_value(home_val))
                    stats.corner_kicks_away = int(self._parse_stat_value(away_val))
                elif _match("offsides", "offside"):
                    stats.offsides_home = int(self._parse_stat_value(home_val))
                    stats.offsides_away = int(self._parse_stat_value(away_val))
                elif _match("fouls", "foul"):
                    stats.fouls_home = int(self._parse_stat_value(home_val))
                    stats.fouls_away = int(self._parse_stat_value(away_val))
                elif _match("yellowcards", "yellow cards", "yellow card"):
                    stats.yellow_cards_home = int(self._parse_stat_value(home_val))
                    stats.yellow_cards_away = int(self._parse_stat_value(away_val))
                elif _match("redcards", "red cards", "red card"):
                    stats.red_cards_home = int(self._parse_stat_value(home_val))
                    stats.red_cards_away = int(self._parse_stat_value(away_val))
                elif _match("accuratepasses", "passes accurate", "accurate passes"):
                    # Display string is usually "330/420 (79%)" → we grab both sides.
                    accurate_h, total_h = self._parse_ratio(home_display or home_val)
                    accurate_a, total_a = self._parse_ratio(away_display or away_val)
                    stats.accurate_passes_home = int(accurate_h)
                    stats.accurate_passes_away = int(accurate_a)
                    stats.pass_accuracy_home = self._parse_percent(home_display or home_val)
                    stats.pass_accuracy_away = self._parse_percent(away_display or away_val)
                    if total_h > 0:
                        stats.total_passes_home = int(total_h)
                    if total_a > 0:
                        stats.total_passes_away = int(total_a)
                elif _match("passes", "total passes"):
                    val_h = int(self._parse_stat_value(home_val))
                    val_a = int(self._parse_stat_value(away_val))
                    if stats.total_passes_home == 0:
                        stats.total_passes_home = val_h
                    if stats.total_passes_away == 0:
                        stats.total_passes_away = val_a

        # Derive total shots from components when Sofascore didn't emit it directly.
        if not seen_total_shots:
            derived_home = (
                stats.shots_on_target_home
                + stats.shots_off_target_home
                + stats.blocked_shots_home
            )
            derived_away = (
                stats.shots_on_target_away
                + stats.shots_off_target_away
                + stats.blocked_shots_away
            )
            if derived_home > 0:
                stats.total_shots_home = derived_home
            if derived_away > 0:
                stats.total_shots_away = derived_away

        return stats

    async def get_upcoming_matches(self) -> list[UpcomingMatch]:
        """Fetch today's upcoming (scheduled) football matches."""
        today = datetime.now(TZ_TURKEY).strftime("%Y-%m-%d")
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
                start_time = str(start_ts) if start_ts else "0"

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

    async def get_match_form(self, event_id: str) -> dict:
        """Fetch pregame form for both teams (recent results, league position, rating)."""
        data = await self._fetch_json(f"{SOFASCORE_BASE}/event/{event_id}/pregame-form")
        if not data:
            return {"home": {}, "away": {}}

        def _parse_side(side: dict) -> dict:
            if not isinstance(side, dict):
                return {}
            raw_form = side.get("form") or []
            # Sofascore returns strings like "W","D","L"
            form = [str(x).upper()[:1] for x in raw_form if x]
            return {
                "form": form,
                "position": side.get("position"),
                "value": str(side.get("value", "")).strip(),
                "avg_rating": side.get("avgRating"),
            }

        return {
            "home": _parse_side(data.get("homeTeam", {})),
            "away": _parse_side(data.get("awayTeam", {})),
        }

    async def get_match_votes(self, event_id: str) -> dict:
        """Fetch fan-vote distribution (proxy for audience expectation)."""
        data = await self._fetch_json(f"{SOFASCORE_BASE}/event/{event_id}/votes")
        if not data:
            return {"home_pct": 0, "draw_pct": 0, "away_pct": 0, "total": 0}

        vote = data.get("vote", {})
        v1 = int(vote.get("vote1", 0) or 0)
        vx = int(vote.get("voteX", 0) or 0)
        v2 = int(vote.get("vote2", 0) or 0)
        total = v1 + vx + v2
        if total <= 0:
            return {"home_pct": 0, "draw_pct": 0, "away_pct": 0, "total": 0}
        return {
            "home_pct": round(v1 * 100 / total, 1),
            "draw_pct": round(vx * 100 / total, 1),
            "away_pct": round(v2 * 100 / total, 1),
            "total": total,
        }

    async def get_match_odds(self, event_id: str) -> dict:
        """Fetch featured 1X2 odds for the match (expectation proxy)."""
        data = await self._fetch_json(f"{SOFASCORE_BASE}/event/{event_id}/odds/1/featured")
        if not data:
            return {}

        featured = data.get("featured") or {}
        default = featured.get("default") or {}
        choices = default.get("choices") or []
        odds = {}
        for c in choices:
            name = str(c.get("name", "")).strip()
            frac = c.get("fractionalValue") or c.get("initialFractionalValue")
            # fractionalValue is like "5/2"; convert to decimal
            decimal_val = None
            if frac and "/" in frac:
                try:
                    num, den = frac.split("/", 1)
                    decimal_val = round(int(num) / int(den) + 1, 2)
                except (ValueError, ZeroDivisionError):
                    decimal_val = None
            if name == "1":
                odds["home"] = decimal_val
            elif name == "X":
                odds["draw"] = decimal_val
            elif name == "2":
                odds["away"] = decimal_val
        return odds

    async def get_live_match_details(self, event_id: str) -> dict:
        """Fetch enriched live-match detail payload: stats, form, votes, odds."""
        stats_task = asyncio.create_task(self.get_match_statistics(event_id))
        form_task = asyncio.create_task(self.get_match_form(event_id))
        votes_task = asyncio.create_task(self.get_match_votes(event_id))
        odds_task = asyncio.create_task(self.get_match_odds(event_id))
        results = await asyncio.gather(
            stats_task, form_task, votes_task, odds_task, return_exceptions=True
        )
        stats_res, form_res, votes_res, odds_res = results
        return {
            "stats": (stats_res.to_dict() if hasattr(stats_res, "to_dict") else None),
            "form": form_res if isinstance(form_res, dict) else {"home": {}, "away": {}},
            "votes": votes_res if isinstance(votes_res, dict) else {},
            "odds": odds_res if isinstance(odds_res, dict) else {},
        }

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None


# Singleton scraper instance
scraper = SofascoreScraper()
