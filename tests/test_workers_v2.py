import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import workers
from data_quality import MatchStats
from detector import DetectedSignal, RULE_VERSION
from scraper import LiveMatch


def live(event_id="1", home=0, away=0, minute=60):
    return LiveMatch(event_id, "Home", "Away", home, away, minute, "League", "Live")


class WorkerV2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.observation = AsyncMock(return_value=41)
        self.insert = AsyncMock(return_value=(9, True, 1))
        self.detect = Mock(return_value=[])
        self.telegram = AsyncMock(return_value=None)

    async def _process(self, initial, current=None, stats=None):
        current = current or initial
        stats = stats or MatchStats()
        with (
            patch.object(workers.scraper, "get_match_statistics", AsyncMock(return_value=stats)),
            patch.object(workers.scraper, "get_live_matches", AsyncMock(return_value=[current])),
            patch.object(workers, "store_match_observation", self.observation),
            patch.object(workers, "insert_anomaly", self.insert),
            patch.object(workers, "detect_anomalies_detailed", self.detect),
            patch.object(workers, "send_telegram", self.telegram),
        ):
            return await workers._process_live_match(initial)

    async def test_slow_match_does_not_block_ready_match(self):
        slow_started = asyncio.Event()
        ready_processed = asyncio.Event()
        release_slow = asyncio.Event()

        async def process(match):
            if match.event_id == "slow":
                slow_started.set()
                await release_slow.wait()
            else:
                ready_processed.set()
            return 0, None

        matches = [live("slow"), live("ready")]
        with (
            patch.object(workers.scraper, "get_live_matches", AsyncMock(return_value=matches)),
            patch.object(workers, "_process_live_match", side_effect=process),
            patch.object(workers, "_scan_lock", asyncio.Lock()),
            patch.object(workers, "mark_upcoming_anomaly", AsyncMock()),
        ):
            task = asyncio.create_task(workers.anomaly_scan())
            await slow_started.wait()
            await asyncio.wait_for(ready_processed.wait(), timeout=0.2)
            release_slow.set()
            await task

    async def test_state_is_rechecked_before_detector(self):
        initial = live("stale", home=1, away=1, minute=73)
        current = live("stale", home=2, away=1, minute=75)
        stats = MatchStats()
        await self._process(initial, current, stats)
        self.detect.assert_called_once()
        self.assertEqual(self.detect.call_args.args[0].score_home, 2)
        self.assertEqual(self.detect.call_args.args[0].score_away, 1)

    async def test_invalid_observation_is_stored_but_not_detected(self):
        stats = MatchStats(validation_status="INVALID", validation_errors=["bad"])
        await self._process(live(), stats=stats)
        self.observation.assert_awaited_once()
        self.assertEqual(self.observation.await_args.kwargs["validation_status"], "INVALID")
        self.detect.assert_not_called()
        self.insert.assert_not_awaited()

    async def test_partial_stats_remain_nullable(self):
        stats = MatchStats(total_shots_home=5, validation_status="PARTIAL")
        await self._process(live(), stats=stats)
        normalized = self.observation.await_args.kwargs["normalized_stats"]
        self.assertIsNone(normalized["total_shots_away"])
        self.assertIn("total_shots_away", self.observation.await_args.kwargs["missing_fields"])
        self.detect.assert_called_once()

    async def test_signal_observation_and_v2_insert_fields_are_forwarded(self):
        signal = DetectedSignal("A", "away", ["pressure"], ["QUALITY", "VOLUME"])
        self.detect.return_value = [signal]
        stats = MatchStats(period="ALL", fetched_at=12.5, validation_status="PARTIAL")
        await self._process(live("signal"), stats=stats)
        self.observation.assert_awaited_once()
        observation = self.observation.await_args.kwargs
        self.assertEqual(observation["decision_outcome"], "anomaly")
        self.assertEqual(observation["decision_condition"], "A")
        self.assertEqual(observation["selected_side"], "away")
        self.assertEqual(observation["triggered_groups"], ["QUALITY", "VOLUME"])
        self.assertEqual(observation["decision_reasons"], ["pressure"])
        self.insert.assert_awaited_once()
        kwargs = self.insert.await_args.kwargs
        self.assertEqual(kwargs["observation_id"], 41)
        self.assertEqual(kwargs["selected_side"], "away")
        self.assertEqual(kwargs["triggered_groups"], ["QUALITY", "VOLUME"])
        self.assertEqual(kwargs["rule_version"], RULE_VERSION)
        self.assertEqual(kwargs["stats_period"], "ALL")
        self.assertEqual(kwargs["stats_fetched_at"], 12.5)

    async def test_minute_none_is_observed_but_never_detected(self):
        stats = MatchStats()
        await self._process(live("unknown", minute=None), stats=stats)
        self.observation.assert_awaited_once()
        self.detect.assert_not_called()

    async def test_only_reliable_30_to_85_matches_are_processed(self):
        matches = [live("early", minute=29), live("valid", minute=30),
                   live("late", minute=85), live("too-late", minute=86),
                   live("unknown", minute=None)]
        processed = []

        async def process(match):
            processed.append(match.event_id)
            return 0, None

        with (
            patch.object(workers.scraper, "get_live_matches", AsyncMock(return_value=matches)),
            patch.object(workers, "_process_live_match", side_effect=process),
            patch.object(workers, "_scan_lock", asyncio.Lock()),
        ):
            await workers.anomaly_scan()
        self.assertEqual(set(processed), {"valid", "late"})

    async def test_observation_contains_live_match_metadata(self):
        match = live("meta", home=1, away=0, minute=64)
        stats = MatchStats(period="2ND", fetched_at=9.0)
        await self._process(match, stats=stats)
        kwargs = self.observation.await_args.kwargs
        self.assertEqual(kwargs["event_id"], "meta")
        self.assertEqual(kwargs["score_home"], 1)
        self.assertEqual(kwargs["score_away"], 0)
        self.assertEqual(kwargs["minute"], 64)
        self.assertEqual(kwargs["period"], "2ND")
        self.assertEqual(kwargs["event_fetched_at"], kwargs["decision_at"])


if __name__ == "__main__":
    unittest.main()
