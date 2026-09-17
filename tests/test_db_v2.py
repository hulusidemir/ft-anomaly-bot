import os
import tempfile
import unittest

import db


class DatabaseV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.previous_path = db.DATABASE_PATH
        db.DATABASE_PATH = self.database_path
        await db.init_db()

    async def asyncTearDown(self):
        await db.close_db()
        db.DATABASE_PATH = self.previous_path
        os.unlink(self.database_path)

    async def test_first_signal_evidence_is_immutable_on_duplicate_insert(self):
        first = await db.insert_anomaly(
            "event-1", "Home", "Away", 0, 1, 55, "League", "B",
            ["first reason"], {"total_shots_home": 12},
            selected_side="home", triggered_groups=["ATTACKING_CORE"],
            missing_fields=["expected_goals_away"], stats_period="ALL",
            event_fetched_at=10.0, stats_fetched_at=11.0, decision_at=12.0,
            rule_version="v1", observation_id=7,
        )
        second = await db.insert_anomaly(
            "event-1", "Home", "Away", 0, 1, 88, "League", "B",
            ["later reason"], {"total_shots_home": 99},
            selected_side="away", triggered_groups=["RECENT_PRESSURE"],
            missing_fields=[], stats_period="2ND", event_fetched_at=20.0,
            stats_fetched_at=21.0, decision_at=22.0, rule_version="v2",
            observation_id=8,
        )

        self.assertEqual(first[0], second[0])
        self.assertTrue(first[1])
        self.assertFalse(second[1])
        row = (await db.get_anomalies())[0]
        self.assertEqual(row["minute"], 55)
        self.assertEqual(row["selected_side"], "home")
        self.assertEqual(row["stats_snapshot"], '{"total_shots_home": 12}')
        self.assertEqual(row["triggered_rules"], '["first reason"]')
        self.assertEqual(row["triggered_groups"], '["ATTACKING_CORE"]')
        self.assertEqual(row["missing_fields"], '["expected_goals_away"]')
        self.assertEqual(row["stats_period"], "ALL")
        self.assertEqual(row["event_fetched_at"], 10.0)
        self.assertEqual(row["stats_fetched_at"], 11.0)
        self.assertEqual(row["decision_at"], 12.0)
        self.assertEqual(row["rule_version"], "v1")
        self.assertEqual(row["observation_id"], 7)

    async def test_legacy_v2_columns_remain_null_and_explicit_side_grades_result(self):
        await db.insert_anomaly(
            "legacy", "Home", "Away", 0, 0, 40, "League", "A", ["legacy"], {}
        )
        legacy = (await db.get_anomalies())[0]
        self.assertIsNone(legacy["selected_side"])
        self.assertIsNone(legacy["rule_version"])
        self.assertIsNone(legacy["observation_id"])

        await db.insert_anomaly(
            "explicit", "Home", "Away", 0, 1, 60, "League", "B", ["reason"], {},
            selected_side="home", rule_version="v2",
        )
        await db.finalize_match_anomalies("explicit", 1, 0)
        rows = await db.get_deleted_anomalies("successful")
        self.assertEqual([row["match_id"] for row in rows], ["explicit"])

    async def test_observations_are_append_only_and_latest_does_not_regress(self):
        first_id = await db.store_match_observation(
            event_id="event-2", observed_at=200.0, minute=70, period="2ND",
            score_home=1, score_away=0, match_status="inprogress", league="League",
            home_team="Home", away_team="Away", normalized_stats={"shots": 8},
            missing_fields=[], validation_status="VALID",
        )
        older_id = await db.store_match_observation(
            event_id="event-2", observed_at=100.0, minute=40, period="1ST",
            score_home=0, score_away=0, match_status="inprogress", league="League",
            home_team="Home", away_team="Away", normalized_stats={"shots": 1},
            missing_fields=[], validation_status="PARTIAL",
        )
        newer_id = await db.store_match_observation(
            event_id="event-2", observed_at=300.0, minute=80, period="2ND",
            score_home=1, score_away=1, match_status="inprogress", league="League",
            home_team="Home", away_team="Away", normalized_stats={"shots": 12},
            missing_fields=[], validation_status="VALID",
        )

        history = await db.get_recent_observations("event-2", limit=10)
        self.assertEqual({row["id"] for row in history}, {first_id, older_id, newer_id})
        latest = await db.get_latest_match_state("event-2")
        self.assertEqual(latest["id"], newer_id)
        self.assertEqual(latest["observed_at"], 300.0)
        self.assertEqual(latest["normalized_stats"], {"shots": 12})


if __name__ == "__main__":
    unittest.main()
