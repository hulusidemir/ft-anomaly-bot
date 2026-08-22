import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import db
from scraper import MatchResult
from signal_evaluator import evaluate_signal_result, infer_dominant_side
from workers import finished_match_scan


HOME_DOMINANT_STATS = {
    "possession_home": 62,
    "possession_away": 38,
    "total_shots_home": 16,
    "total_shots_away": 6,
    "shots_on_target_home": 7,
    "shots_on_target_away": 2,
    "expected_goals_home": 1.8,
    "expected_goals_away": 0.5,
}


class SignalEvaluatorTests(unittest.TestCase):
    def test_condition_b_selects_trailing_team(self):
        self.assertEqual(infer_dominant_side("B", 2, 1, {}), "away")
        self.assertEqual(infer_dominant_side("B", 0, 1, {}), "home")

    def test_condition_a_prefers_quality_first_statistical_leader(self):
        self.assertEqual(
            infer_dominant_side("A", 0, 0, HOME_DOMINANT_STATS), "home"
        )

    def test_draw_is_a_failed_win_bet(self):
        self.assertEqual(evaluate_signal_result("home", 2, 1), "successful")
        self.assertEqual(evaluate_signal_result("home", 1, 1), "failed")
        self.assertEqual(evaluate_signal_result("away", 2, 1), "failed")
        self.assertEqual(evaluate_signal_result("unknown", 2, 1), "unresolved")


class FinishedMatchDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_path = db.DATABASE_PATH
        self.previous_db = db._db
        handle, self.database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db.DATABASE_PATH = self.database_path
        db._db = None
        await db.init_db()

    async def asyncTearDown(self):
        await db.close_db()
        db.DATABASE_PATH = self.previous_path
        db._db = self.previous_db
        os.unlink(self.database_path)

    async def _insert_home_signal(self, event_id="100"):
        row_id, is_new, _ = await db.insert_anomaly(
            match_id=event_id,
            home_team="Home",
            away_team="Away",
            score_home=0,
            score_away=0,
            minute=55,
            league="League",
            condition_type="A",
            triggered_rules=["home pressure", "home shots"],
            stats_snapshot=HOME_DOMINANT_STATS,
        )
        self.assertTrue(is_new)
        self.assertIsNotNone(row_id)
        return row_id

    async def test_finalize_grades_and_archives_active_signal(self):
        await self._insert_home_signal()

        archived = await db.finalize_match_anomalies("100", 2, 1)

        self.assertEqual(archived, 1)
        self.assertEqual(await db.get_anomalies(), [])
        deleted = await db.get_deleted_anomalies("successful")
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["dominant_side"], "home")
        self.assertEqual(deleted[0]["result_status"], "successful")
        self.assertEqual(deleted[0]["final_score_home"], 2)
        self.assertEqual(deleted[0]["deletion_reason"], "match_finished")

        summary = await db.get_deleted_anomaly_summary()
        self.assertEqual(summary["successful"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["success_rate"], 100.0)

    async def test_finished_signal_cannot_be_restored_to_active_list(self):
        row_id = await self._insert_home_signal()
        await db.finalize_match_anomalies("100", 1, 1)

        restored = await db.restore_anomalies([row_id])

        self.assertEqual(restored, 0)
        self.assertEqual(len(await db.get_deleted_anomalies("failed")), 1)

    async def test_unknown_legacy_prediction_is_excluded_from_success_rate(self):
        await db.insert_anomaly(
            match_id="200",
            home_team="Home",
            away_team="Away",
            score_home=0,
            score_away=0,
            minute=50,
            league="League",
            condition_type="A",
            triggered_rules=["legacy"],
            stats_snapshot={},
        )

        await db.finalize_match_anomalies("200", 2, 0)

        self.assertEqual(len(await db.get_deleted_anomalies("unresolved")), 1)
        summary = await db.get_deleted_anomaly_summary()
        self.assertEqual(summary["unresolved"], 1)
        self.assertEqual(summary["evaluated"], 0)
        self.assertEqual(summary["success_rate"], 0.0)

    async def test_match_action_updates_existing_and_future_signals(self):
        first_id = await self._insert_home_signal("300")
        second_id, is_new, alert_number = await db.insert_anomaly(
            match_id="300",
            home_team="Home",
            away_team="Away",
            score_home=1,
            score_away=0,
            minute=63,
            league="League",
            condition_type="B",
            triggered_rules=["away pressure"],
            stats_snapshot={},
        )
        self.assertTrue(is_new)
        self.assertEqual(alert_number, 2)

        updated = await db.update_anomaly_status(first_id, "ignored")

        self.assertEqual(updated, 2)
        rows = await db.get_anomalies()
        self.assertEqual({row["status"] for row in rows}, {"ignored"})

        third_id, is_new, alert_number = await db.insert_anomaly(
            match_id="300",
            home_team="Home",
            away_team="Away",
            score_home=1,
            score_away=1,
            minute=71,
            league="League",
            condition_type="A",
            triggered_rules=["home pressure"],
            stats_snapshot=HOME_DOMINANT_STATS,
        )
        self.assertTrue(is_new)
        self.assertIsNotNone(third_id)
        self.assertEqual(alert_number, 3)
        rows = await db.get_anomalies()
        self.assertEqual({row["status"] for row in rows}, {"ignored"})
        self.assertEqual({row["alert_number"] for row in rows}, {1, 2, 3})


class FinishedMatchWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_only_finalizes_finished_events(self):
        results = {
            "1": MatchResult("1", True, 3, 1, "finished", "Ended"),
            "2": MatchResult("2", False, 1, 0, "inprogress", "2nd half"),
        }

        with (
            patch(
                "workers.get_pending_anomaly_match_ids",
                new=AsyncMock(return_value=["1", "2"]),
            ),
            patch(
                "workers.scraper.get_match_result",
                new=AsyncMock(side_effect=lambda event_id: results[event_id]),
            ),
            patch(
                "workers.finalize_match_anomalies", new=AsyncMock(return_value=2)
            ) as finalize,
        ):
            report = await finished_match_scan()

        self.assertTrue(report["ok"])
        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["matches_finished"], 1)
        self.assertEqual(report["archived"], 2)
        finalize.assert_awaited_once_with("1", 3, 1)


if __name__ == "__main__":
    unittest.main()
