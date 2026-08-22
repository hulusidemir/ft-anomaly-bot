import hashlib
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from config import TZ_TURKEY
from scraper import SofascoreScraper


class _JsonResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _CategorySession:
    def __init__(self, payloads: dict[str, dict]):
        self.payloads = payloads
        self.headers_seen = []

    async def get(self, url: str, headers: dict | None = None):
        self.headers_seen.append(headers or {})
        category_id = url.split("/category/", 1)[1].split("/", 1)[0]
        return _JsonResponse(self.payloads[category_id])


class SofascoreScraperTests(unittest.IsolatedAsyncioTestCase):
    def test_requested_with_token_matches_frontend_algorithm(self):
        now = 1_785_837_600
        expected = hashlib.sha256(str(now // 1800).encode()).hexdigest()[:6]

        with patch("scraper.time.time", return_value=now):
            self.assertEqual(SofascoreScraper._requested_with_token(), expected)

    async def test_category_fallback_deduplicates_events(self):
        scraper = SofascoreScraper()
        scraper._fetch_json = AsyncMock(return_value={
            "categories": [
                {"category": {"id": 8}, "totalEvents": 2},
                {"category": {"id": 9}, "totalEvents": 1},
                {"category": {"id": 10}, "totalEvents": 0},
            ]
        })
        session = _CategorySession({
            "8": {"events": [{"id": 100}, {"id": 101}]},
            "9": {"events": [{"id": 101}, {"id": 102}]},
        })
        scraper._get_session = AsyncMock(return_value=session)

        result = await scraper._fetch_upcoming_by_category("2026-08-04")

        self.assertEqual({event["id"] for event in result["events"]}, {100, 101, 102})
        self.assertEqual(scraper._get_session.await_count, 2)
        self.assertTrue(all("X-Requested-With" in headers for headers in session.headers_seen))

    async def test_upcoming_parser_keeps_only_rolling_next_24_hours(self):
        scraper = SofascoreScraper()
        now = int(
            datetime.now(TZ_TURKEY)
            .replace(hour=12, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        future = now + 3600
        stale = now - scraper.UPCOMING_START_GRACE_SECONDS - 1
        next_day = now + 20 * 3600
        too_late = now + 25 * 3600

        def event(event_id: int, start_timestamp: int) -> dict:
            return {
                "id": event_id,
                "status": {"type": "notstarted"},
                "homeTeam": {"name": "Home"},
                "awayTeam": {"name": "Away"},
                "tournament": {"name": "League", "category": {"name": "Country"}},
                "startTimestamp": start_timestamp,
                "roundInfo": {"round": 3},
            }

        scraper._fetch_upcoming_by_category = AsyncMock(return_value={
            "events": [
                event(1, future),
                event(2, stale),
                event(3, next_day),
                event(4, too_late),
            ]
        })

        with patch("scraper.time.time", return_value=now):
            matches = await scraper.get_upcoming_matches()

        self.assertEqual([match.event_id for match in matches], ["1", "3"])
        self.assertEqual(matches[0].round_info, "Round 3")
        requested_dates = {
            call.args[0] for call in scraper._fetch_upcoming_by_category.await_args_list
        }
        self.assertEqual(len(requested_dates), 2)

    async def test_last_failed_attempt_does_not_sleep_or_rotate(self):
        scraper = SofascoreScraper()
        scraper._warm_session = AsyncMock()
        scraper._throttle = AsyncMock()
        scraper._rotate_session = AsyncMock()
        session = AsyncMock()
        session.get.return_value = _JsonResponse({}, status_code=404)
        scraper._get_session = AsyncMock(return_value=session)

        with patch("scraper.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await scraper._fetch_json(
                "https://example.test/events/live", retries=1
            )

        self.assertIsNone(result)
        scraper._rotate_session.assert_not_awaited()
        sleep.assert_not_awaited()

    async def test_match_result_requires_finished_status_and_reads_final_score(self):
        scraper = SofascoreScraper()
        scraper._fetch_json = AsyncMock(return_value={
            "event": {
                "id": 42,
                "status": {"type": "finished", "description": "Ended"},
                "homeScore": {"current": 2},
                "awayScore": {"current": 1},
            }
        })

        result = await scraper.get_match_result("42")

        self.assertIsNotNone(result)
        self.assertTrue(result.is_finished)
        self.assertEqual((result.score_home, result.score_away), (2, 1))
        scraper._fetch_json.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
