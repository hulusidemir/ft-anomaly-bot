"""Regression tests for the v2 SofaScore normalization contract."""

import unittest
from unittest.mock import AsyncMock, patch

from scraper import SofascoreScraper


def _item(key, home=None, away=None, *, name=None):
    item = {"key": key, "name": name or key}
    if home is not None:
        item["homeValue"] = home
    if away is not None:
        item["awayValue"] = away
    return item


def _statistics(items, period="ALL"):
    return {
        "statistics": [{
            "period": period,
            "groups": [{"statisticsItems": items}],
        }]
    }


class StatisticsNormalizationV2Tests(unittest.IsolatedAsyncioTestCase):
    async def _parse(self, items, period="ALL"):
        scraper = SofascoreScraper()
        scraper._fetch_json = AsyncMock(return_value=_statistics(items, period))
        result = await scraper.get_match_statistics("42")
        scraper._fetch_json.assert_awaited_once()
        return result

    async def test_total_shots_is_not_overwritten_by_inside_or_outside_box(self):
        canonical = _item("totalShotsOnGoal", 22, 10)
        distractors = [
            _item("totalShotsInsideBox", 9, 4),
            _item("totalShotsOutsideBox", 13, 6),
            _item("shotsOnGoal", 7, 3),
            _item("shotsOffGoal", 9, 4),
            _item("blockedScoringAttempt", 6, 3),
        ]

        first = await self._parse([canonical, *distractors])
        last = await self._parse([*reversed(distractors), canonical])

        for stats in (first, last):
            self.assertEqual((stats.total_shots_home, stats.total_shots_away), (22, 10))
            self.assertEqual((stats.shots_on_target_home, stats.shots_on_target_away), (7, 3))
            self.assertNotEqual(stats.validation_status, "INVALID")

    async def test_conflicting_total_shot_aliases_are_invalid_not_order_dependent(self):
        aliases = [
            _item("totalShotsOnGoal", 22, 10),
            _item("totalShots", 9, 4),
        ]
        first = await self._parse(aliases)
        last = await self._parse(list(reversed(aliases)))

        for stats in (first, last):
            self.assertIsNone(stats.total_shots_home)
            self.assertIsNone(stats.total_shots_away)
            self.assertEqual(stats.validation_status, "INVALID")
            self.assertTrue(stats.validation_errors)

    async def test_accurate_pass_count_is_not_interpreted_as_percentage(self):
        count_only = await self._parse([_item("accuratePasses", 490, 162)])
        self.assertEqual((count_only.accurate_passes_home, count_only.accurate_passes_away), (490, 162))
        self.assertIsNone(count_only.pass_accuracy_home)
        self.assertIsNone(count_only.pass_accuracy_away)

        with_totals = await self._parse([
            _item("accuratePasses", 100, 81),
            _item("totalPasses", 200, 162),
        ])
        self.assertEqual((with_totals.pass_accuracy_home, with_totals.pass_accuracy_away), (50.0, 50.0))

    async def test_provider_zero_is_distinct_from_missing(self):
        stats = await self._parse([_item("totalShotsOnGoal", 0, None)])

        self.assertEqual(stats.total_shots_home, 0)
        self.assertIsNone(stats.total_shots_away)
        self.assertNotIn("total_shots_home", stats.missing_fields)
        self.assertIn("total_shots_away", stats.missing_fields)
        self.assertEqual(stats.field_status["total_shots_home"], "observed")
        self.assertEqual(stats.field_status["total_shots_away"], "missing")
        self.assertEqual(stats.validation_status, "PARTIAL")

    async def test_second_half_only_data_is_partial_and_retains_its_period(self):
        stats = await self._parse([_item("totalShotsOnGoal", 4, 2)], period="2ND")

        self.assertEqual(stats.period, "2ND")
        self.assertEqual((stats.total_shots_home, stats.total_shots_away), (4, 2))
        self.assertEqual(stats.validation_status, "PARTIAL")
        self.assertIsNotNone(stats.fetched_at)

    async def test_shots_on_target_above_total_is_invalid(self):
        stats = await self._parse([
            _item("totalShotsOnGoal", 4, 3),
            _item("shotsOnGoal", 5, 2),
        ])

        self.assertEqual(stats.validation_status, "INVALID")
        self.assertTrue(stats.validation_errors)


class MinuteParsingV2Tests(unittest.TestCase):
    NOW = 1_800_000_000

    def setUp(self):
        self.scraper = SofascoreScraper()

    def _minute(self, event):
        with patch("scraper.time.time", return_value=self.NOW):
            return self.scraper._parse_minute(event)

    def test_trusted_first_and_second_half_clocks(self):
        self.assertEqual(self._minute({
            "status": {"type": "inprogress", "period": "period1"},
            "statusTime": {"initial": 0, "timestamp": self.NOW - 33 * 60},
        }), 33)
        self.assertEqual(self._minute({
            "status": {"type": "inprogress", "period": "period2"},
            "statusTime": {"timestamp": self.NOW - 7 * 60},
        }), 52)

    def test_halftime_and_first_half_stoppage_have_safe_minutes(self):
        self.assertEqual(self._minute({
            "status": {"type": "inprogress", "description": "Halftime"},
        }), 45)
        self.assertEqual(self._minute({
            "status": {"type": "inprogress", "period": "period1"},
            "statusTime": {"initial": 0, "timestamp": self.NOW - 47 * 60},
        }), 47)

    def test_untrusted_or_stopped_match_times_are_unknown(self):
        cases = [
            {"status": {"type": "inprogress", "description": "Live"},
             "statusTime": {"timestamp": self.NOW - 7 * 60}},
            {"status": {"type": "inprogress", "description": "Paused"},
             "startTimestamp": self.NOW - 70 * 60},
            {"status": {"type": "inprogress", "description": "Interrupted"},
             "startTimestamp": self.NOW - 70 * 60},
            {"status": {"type": "inprogress", "description": "Restarted"},
             "startTimestamp": self.NOW - 70 * 60},
            {"status": {"type": "inprogress", "description": "Live"}},
        ]
        for event in cases:
            with self.subTest(event=event):
                self.assertIsNone(self._minute(event))


if __name__ == "__main__":
    unittest.main()
