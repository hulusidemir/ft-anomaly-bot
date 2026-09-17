import asyncio
import hashlib
import random
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from curl_cffi.requests import AsyncSession
from config import SOFASCORE_BASE, TZ_TURKEY
from data_quality import MatchStats, normalize_statistics

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


@dataclass
class MatchResult:
    event_id: str
    is_finished: bool
    score_home: int | None
    score_away: int | None
    status_type: str
    status_desc: str


class SofascoreScraper:
    # Concurrency: keep it low – a real browser never fires dozens of parallel XHRs.
    MAX_CONCURRENT_REQUESTS = 2
    # The aggregate daily endpoint was retired in mid-2026.  Its replacement
    # requires one request per category, so use a separate, bounded pool for
    # that short-lived batch instead of the detail-request throttle.
    MAX_CATEGORY_REQUESTS = 6
    # Keep briefly delayed fixtures, but do not surface stale not-started rows
    # from earlier in the day as upcoming matches.
    UPCOMING_START_GRACE_SECONDS = 30 * 60
    # Jittered gap between any two requests (seconds).
    MIN_GAP_RANGE = (1.8, 3.6)
    # Re-warm session if older than this (seconds).
    WARMUP_TTL = 900
    # If warm-up fails, let API requests proceed for a short period instead of
    # making every concurrent caller retry the homepage request.
    WARMUP_RETRY_COOLDOWN = 30

    def __init__(self):
        self._session: AsyncSession | None = None
        self._rotate_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._warm_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_REQUESTS)
        self._last_request_time = 0.0
        self._last_rotate_time: float | None = None
        self._impersonate = random.choice(IMPERSONATE_TARGETS)
        self._accept_lang = random.choice(ACCEPT_LANGUAGES)
        self._session_warm_at = 0.0
        self._session_warm_attempt_at = 0.0
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
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": self._accept_lang,
                "Referer": f"{SOFASCORE_WEB}/",
                "Origin": SOFASCORE_WEB,
                "Cache-Control": "no-cache",
            },
            timeout=20,
        )

    @staticmethod
    def _requested_with_token() -> str:
        """Return the rolling token used by Sofascore's web client.

        The frontend hashes the current 30-minute Unix bucket and sends the
        first six hex characters as ``X-Requested-With``.  A conventional
        ``XMLHttpRequest`` value now causes some API routes to return a masked
        404 response.
        """
        bucket = int(time.time()) // 1800
        return hashlib.sha256(str(bucket).encode()).hexdigest()[:6]

    async def _get_session(self) -> AsyncSession:
        async with self._rotate_lock:
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
        if (
            self._session_warm_attempt_at
            and (now - self._session_warm_attempt_at) < self.WARMUP_RETRY_COOLDOWN
        ):
            return

        async with self._warm_lock:
            now = time.monotonic()
            if self._session_warm_at and (now - self._session_warm_at) < self.WARMUP_TTL:
                return
            if (
                self._session_warm_attempt_at
                and (now - self._session_warm_attempt_at) < self.WARMUP_RETRY_COOLDOWN
            ):
                return

            self._session_warm_attempt_at = now
            try:
                sess = await self._get_session()
                resp = await sess.get(SOFASCORE_WEB, timeout=15)
                if resp.status_code in (200, 304) and sess is self._session:
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

    async def _rotate_session(self, failed_session: AsyncSession | None = None) -> None:
        """Coalesce concurrent failures instead of queueing repeated rotations."""
        async with self._rotate_lock:
            if failed_session is not None and failed_session is not self._session:
                return  # This response belongs to an already retired session.
            now = time.monotonic()
            if self._last_rotate_time is not None and now - self._last_rotate_time < 5:
                return

            old_session = self._session
            self._session = None
            self._last_rotate_time = now
            others = [t for t in IMPERSONATE_TARGETS if t != self._impersonate]
            if others:
                self._impersonate = random.choice(others)
            self._accept_lang = random.choice(ACCEPT_LANGUAGES)
            self._session_warm_at = 0.0
            self._session_warm_attempt_at = 0.0
            self._consecutive_bot_errors = 0
            if old_session is not None:
                try:
                    await old_session.close()
                except Exception:
                    logger.debug("Retired session close failed", exc_info=True)
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
                session = None
                try:
                    session = await self._get_session()
                    resp = await session.get(
                        url,
                        headers={"X-Requested-With": self._requested_with_token()},
                    )
                except Exception as e:
                    self.last_fetch_error = {
                        "url": url,
                        "status": None,
                        "message": f"Request error: {e}",
                    }
                    logger.warning(f"Request error on {url}: {e}")
                    if attempt + 1 >= retries:
                        break
                    await self._rotate_session(session)
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
                if attempt + 1 >= retries:
                    logger.warning(f"Rate limited (429) on {url}; retries exhausted")
                    break
                wait = (2 ** attempt) * 5 + random.uniform(2, 6)
                logger.warning(f"Rate limited (429) on {url}, waiting {wait:.1f}s")
                await asyncio.sleep(wait)
                if attempt >= 1:
                    await self._rotate_session(session)
                continue

            if status == 403:
                self._consecutive_bot_errors += 1
                self.last_fetch_error = {
                    "url": url,
                    "status": status,
                    "message": "Forbidden by Sofascore",
                }
                if attempt + 1 >= retries:
                    logger.warning(f"Forbidden (403) on {url}; retries exhausted")
                    break
                wait = (2 ** attempt) * 3 + random.uniform(3, 7)
                logger.warning(f"Forbidden (403) on {url} – rotating session")
                await self._rotate_session(session)
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
                    if attempt + 1 >= retries:
                        logger.warning(
                            f"404 on list endpoint {url}; retries exhausted"
                        )
                        break
                    wait = (2 ** attempt) * 3 + random.uniform(2, 5)
                    logger.warning(
                        f"404 on list endpoint {url} – treating as bot-protection, "
                        f"rotating and retrying in {wait:.1f}s"
                    )
                    await self._rotate_session(session)
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
                    await self._rotate_session(session)
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
                if attempt + 1 >= retries:
                    logger.warning(f"Server error ({status}) on {url}; retries exhausted")
                    break
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

    def _parse_minute(self, event: dict) -> int | None:
        """Extract current match minute from event data.

        SofaScore provides:
          statusTime.initial  – elapsed seconds at period start (0 for 1st half, 2700 for 2nd)
          statusTime.timestamp – UNIX timestamp when the current period clock started
        Formula: minute = (initial + (now - timestamp)) / 60
        """
        now = int(time.time())

        status = event.get("status", {})
        period = status.get("period")

        # Use period clocks only when SofaScore identifies the current half.
        status_time = event.get("statusTime", {})
        ts = status_time.get("timestamp")
        if period in ("period1", "period2") and ts and ts > 0:
            initial = status_time.get("initial")
            if initial is None:
                initial = 0 if period == "period1" else 2700
            elapsed = now - ts
            minute = (initial + max(elapsed, 0)) // 60
            return max(0, min(int(minute), 130))

        # Fallback: the equivalent period clock in the event time payload.
        time_data = event.get("time", {})
        if period in ("period1", "period2") and time_data:
            period_start = time_data.get("currentPeriodStartTimestamp")
            if period_start and period_start > 0:
                initial = time_data.get("initial")
                if initial is None:
                    initial = 0 if period == "period1" else 2700
                elapsed = now - period_start
                minute = (initial + max(elapsed, 0)) // 60
                return max(0, min(int(minute), 130))

        # Halftime has a known boundary even without a running clock.
        status = event.get("status", {})
        if str(status.get("description", "")).lower() == "halftime":
            return 45

        return None

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

    async def get_match_result(self, event_id: str) -> MatchResult | None:
        """Fetch one event and return its final score only when it is finished."""
        data = await self._fetch_json(
            f"{SOFASCORE_BASE}/event/{event_id}", retries=2
        )
        if not data:
            return None

        event = data.get("event") or data
        status = event.get("status") or {}
        status_type = str(status.get("type") or "").lower()
        is_finished = status_type == "finished"
        home_score = event.get("homeScore") or {}
        away_score = event.get("awayScore") or {}

        def _score(score_data: dict) -> int | None:
            value = score_data.get("current")
            if value is None:
                value = score_data.get("normaltime")
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return MatchResult(
            event_id=str(event.get("id") or event_id),
            is_finished=is_finished,
            score_home=_score(home_score),
            score_away=_score(away_score),
            status_type=status_type,
            status_desc=str(status.get("description") or ""),
        )

    async def get_match_statistics(self, event_id: str) -> MatchStats | None:
        """Fetch detailed statistics for a specific match."""
        data = await self._fetch_json(f"{SOFASCORE_BASE}/event/{event_id}/statistics")
        if not data:
            return None
        return normalize_statistics(data, fetched_at=time.time())

    async def _fetch_upcoming_by_category(self, date: str) -> dict | None:
        """Fetch a daily schedule through the category endpoints.

        Sofascore's former ``sport/.../scheduled-events`` aggregate endpoint
        now returns 404, while the category index and per-category schedules
        remain available.  Fetch those schedules concurrently and de-duplicate
        the resulting events.
        """
        offset = int(datetime.now(TZ_TURKEY).utcoffset().total_seconds())
        index_url = f"{SOFASCORE_BASE}/sport/football/{date}/{offset}/categories"
        index_data = await self._fetch_json(index_url, retries=2)
        if not index_data:
            return None

        category_ids = []
        for entry in index_data.get("categories", []):
            category = entry.get("category") or {}
            category_id = category.get("id")
            if category_id is not None and entry.get("totalEvents", 0):
                category_ids.append(str(category_id))

        category_ids = list(dict.fromkeys(category_ids))
        if not category_ids:
            self.last_fetch_error = None
            return {"events": []}

        semaphore = asyncio.Semaphore(self.MAX_CATEGORY_REQUESTS)

        async def _fetch_category(category_id: str) -> dict | None:
            url = f"{SOFASCORE_BASE}/category/{category_id}/scheduled-events/{date}"
            for attempt in range(2):
                try:
                    async with semaphore:
                        session = await self._get_session()
                        response = await session.get(
                            url,
                            headers={"X-Requested-With": self._requested_with_token()},
                        )
                    if response.status_code == 200:
                        return response.json()
                    logger.warning(
                        "Category schedule returned HTTP %s: %s",
                        response.status_code,
                        url,
                    )
                except Exception as exc:
                    logger.warning("Category schedule request failed (%s): %s", url, exc)

                if attempt == 0:
                    await asyncio.sleep(random.uniform(0.3, 0.8))
            return None

        results = await asyncio.gather(
            *(_fetch_category(category_id) for category_id in category_ids),
            return_exceptions=True,
        )

        events_by_id: dict[str, dict] = {}
        successful = 0
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Category schedule failed: %s", result)
                continue
            if result is None:
                continue
            successful += 1
            for event in result.get("events", []):
                event_id = event.get("id")
                if event_id is not None:
                    events_by_id[str(event_id)] = event

        if successful == 0:
            self.last_fetch_error = {
                "url": index_url,
                "status": None,
                "message": "All category schedule requests failed",
            }
            return None

        if successful < len(category_ids):
            logger.warning(
                "Upcoming schedule is partial: %s/%s categories fetched",
                successful,
                len(category_ids),
            )

        self.last_fetch_error = None
        return {"events": list(events_by_id.values())}

    async def get_upcoming_matches(self) -> list[UpcomingMatch]:
        """Fetch not-started football matches in the rolling next 24 hours."""
        now_ts = int(time.time())
        horizon_ts = now_ts + 24 * 60 * 60
        schedule_dates = sorted({
            datetime.fromtimestamp(now_ts, tz=TZ_TURKEY).strftime("%Y-%m-%d"),
            datetime.fromtimestamp(horizon_ts, tz=TZ_TURKEY).strftime("%Y-%m-%d"),
        })
        daily_results = await asyncio.gather(
            *(self._fetch_upcoming_by_category(date) for date in schedule_dates)
        )
        successful_results = [data for data in daily_results if data is not None]
        if not successful_results:
            logger.error("Failed to fetch upcoming matches")
            return []

        events_by_id: dict[str, dict] = {}
        for data in successful_results:
            for event in data.get("events", []):
                event_id = event.get("id")
                if event_id is not None:
                    events_by_id[str(event_id)] = event

        matches = []
        for event in events_by_id.values():
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
                if not start_ts:
                    continue
                start_ts = int(start_ts)
                if start_ts < now_ts or start_ts > horizon_ts:
                    continue
                start_time = str(start_ts)

                round_info_data = event.get("roundInfo", {})
                round_str = ""
                round_number = round_info_data.get("round") if round_info_data else None
                if round_number is not None:
                    round_str = f"Round {round_number}"

                event_id = event.get("id")
                if event_id is None:
                    continue

                matches.append(UpcomingMatch(
                    event_id=str(event_id),
                    home_team=home.get("name", "Unknown"),
                    away_team=away.get("name", "Unknown"),
                    league=full_league,
                    start_time=start_time,
                    round_info=round_str,
                ))
            except Exception as e:
                logger.debug(f"Error parsing upcoming event: {e}")
                continue

        return sorted(matches, key=lambda match: int(match.start_time))

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

    async def get_anomaly_match_details(self, event_id: str) -> dict:
        """Fetch enriched anomaly-event details: stats, form, votes, and odds."""
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
