import unittest

from detector import check_condition_a, check_condition_b, condition_a_dominant_side
from scraper import LiveMatch, MatchStats
from signal_evaluator import infer_dominant_side


def match(home=0, away=0, minute=60):
    return LiveMatch('1', 'Home', 'Away', home, away, minute, 'League', '2nd half')


class DetectorTests(unittest.TestCase):
    def test_small_sot_counts_cannot_exploit_zero_denominator(self):
        self.assertEqual(check_condition_a(match(), MatchStats(
            shots_on_target_home=2, expected_goals_home=1.0,
        )), [])

    def test_absolute_sot_edge_works_even_at_high_opponent_count(self):
        rules = check_condition_a(match(), MatchStats(
            shots_on_target_home=9, shots_on_target_away=6,
            expected_goals_home=1.0, expected_goals_away=0,
            corner_kicks_home=5, corner_kicks_away=0,
        ))
        self.assertEqual(len(rules), 2)

    def test_sot_threshold_scales_with_minute(self):
        stats = MatchStats(
            shots_on_target_home=3, shots_on_target_away=0,
            big_chances_home=2, big_chances_away=0,
            corner_kicks_home=5, corner_kicks_away=0,
        )
        self.assertTrue(check_condition_a(match(minute=60), stats))
        self.assertEqual(check_condition_a(match(minute=90), stats), [])

    def test_quality_boundaries_and_correlated_quality_is_one_vote(self):
        for metrics in ({'expected_goals_home': 1.0}, {'big_chances_home': 2}):
            with self.subTest(metrics=metrics):
                self.assertTrue(check_condition_a(match(), MatchStats(
                    shots_on_target_home=3, shots_on_target_away=0,
                    expected_goals_away=0, big_chances_away=0, **metrics,
                    corner_kicks_home=5, corner_kicks_away=0,
                )))
        self.assertEqual(check_condition_a(match(), MatchStats(
            expected_goals_home=2, big_chances_home=4,
        )), [])
        self.assertEqual(check_condition_a(match(), MatchStats(
            shots_on_target_home=3, expected_goals_home=0.99, big_chances_home=1,
        )), [])

    def test_pressure_combines_attacks_and_corners(self):
        stats = MatchStats(
            dangerous_attacks_home=40, dangerous_attacks_away=0,
            corner_kicks_home=5, corner_kicks_away=0,
            expected_goals_home=1.0, expected_goals_away=0,
        )
        self.assertEqual(len(check_condition_a(match(), stats)), 2)
        stats.corner_kicks_home = 4
        self.assertEqual(check_condition_a(match(), stats), [])

    def test_equal_pressure_is_not_dominance(self):
        stats = MatchStats(dangerous_attacks_home=40, corner_kicks_home=4,
                           dangerous_attacks_away=40, corner_kicks_away=4, expected_goals_home=1)
        self.assertEqual(check_condition_a(match(), stats), [])

    def test_opposing_signals_cannot_form_quorum(self):
        self.assertEqual(check_condition_a(match(), MatchStats(
            shots_on_target_home=3, expected_goals_away=1,
        )), [])

    def test_possession_and_volume_do_not_double_count(self):
        self.assertEqual(check_condition_a(match(), MatchStats(
            possession_home=70, possession_away=30, total_shots_home=12,
        )), [])

    def test_away_selection_is_persisted_over_legacy_composite(self):
        stats = MatchStats(
            expected_goals_home=0, expected_goals_away=1,
            dangerous_attacks_home=0, dangerous_attacks_away=40,
            corner_kicks_home=0, corner_kicks_away=5,
            total_shots_home=8, shots_on_target_home=2, possession_home=80,
        )
        self.assertEqual(condition_a_dominant_side(match(), stats), 'away')
        values = stats.to_dict() | {'signal_side': 'away'}
        self.assertEqual(infer_dominant_side('A', 0, 0, values), 'away')

    def test_extreme_quality_bypasses_volume_with_vulnerability(self):
        for metrics in ({'expected_goals_away': 1.5}, {'big_chances_away': 3}):
            with self.subTest(metrics=metrics):
                rules = check_condition_b(match(home=1), MatchStats(
                    red_cards_home=1, red_cards_away=0,
                    expected_goals_home=0, big_chances_home=0, **metrics,
                ))
                self.assertEqual(len(rules), 2)

    def test_quality_below_threshold_and_wrong_side_cards_do_not_trigger(self):
        for stats in (
            MatchStats(expected_goals_away=1.49, big_chances_away=2, yellow_cards_home=4),
            MatchStats(expected_goals_away=1.5, yellow_cards_away=4),
            MatchStats(expected_goals_away=1.5, yellow_cards_home=3),
        ):
            self.assertEqual(check_condition_b(match(home=1), stats), [])

    def test_extreme_quality_alone_remains_one_signal(self):
        self.assertEqual(check_condition_b(match(home=1), MatchStats(expected_goals_away=3)), [])

    def test_existing_volume_and_red_card_path_survives(self):
        stats = MatchStats(
            total_shots_home=12, total_shots_away=4,
            shots_on_target_home=5, shots_on_target_away=2,
            red_cards_home=0, red_cards_away=1,
        )
        self.assertEqual(len(check_condition_b(match(away=1), stats)), 2)

    def test_shot_volume_fallback_works_without_xg_or_big_chances(self):
        stats = MatchStats(
            shots_on_target_home=2, shots_on_target_away=8,
            shots_off_target_home=2, shots_off_target_away=4,
            corner_kicks_home=1, corner_kicks_away=4,
        )

        self.assertTrue(check_condition_b(match(home=1), stats))

    def test_shot_volume_fallback_does_not_treat_missing_values_as_zero(self):
        stats = MatchStats(
            shots_on_target_home=2, shots_on_target_away=8,
            shots_off_target_home=2,
            corner_kicks_home=1, corner_kicks_away=4,
        )

        self.assertEqual(check_condition_b(match(home=1), stats), [])

    def test_late_quality_and_vulnerability_still_work(self):
        stats = MatchStats(
            expected_goals_home=1.5, expected_goals_away=0,
            red_cards_home=0, red_cards_away=1,
            yellow_cards_away=4,
        )
        self.assertTrue(check_condition_b(match(away=1, minute=85), stats))
        stats.yellow_cards_away = 0
        stats.red_cards_away = 0
        stats.dangerous_attacks_home = 50
        stats.dangerous_attacks_away = 0
        self.assertEqual(check_condition_b(match(away=1, minute=85), stats), [])

    def test_score_gates(self):
        stats = MatchStats(shots_on_target_home=8, expected_goals_home=3, red_cards_away=1)
        self.assertEqual(check_condition_a(match(home=1), stats), [])
        self.assertEqual(check_condition_b(match(), stats), [])
        self.assertEqual(check_condition_b(match(away=2), stats), [])

    def test_dangerous_attacks_and_cards_without_attacking_core_do_not_trigger(self):
        stats = MatchStats(
            dangerous_attacks_home=40, dangerous_attacks_away=0,
            yellow_cards_home=4, yellow_cards_away=1,
            red_cards_home=0, red_cards_away=1,
        )
        self.assertEqual(check_condition_b(match(away=1), stats), [])

    def test_signalled_side_red_card_is_not_positive_evidence(self):
        stats = MatchStats(
            expected_goals_home=1.5, expected_goals_away=0,
            red_cards_home=1, red_cards_away=0,
        )
        self.assertEqual(check_condition_b(match(away=1), stats), [])

    def test_missing_opponent_values_do_not_create_superiority(self):
        stats = MatchStats(expected_goals_home=1.5, total_shots_home=12)
        self.assertEqual(check_condition_b(match(away=1), stats), [])

    def test_condition_b_threshold_has_no_minute_80_cliff(self):
        stats = MatchStats(
            total_shots_home=16, total_shots_away=10,
            shots_on_target_home=8, shots_on_target_away=5,
            red_cards_home=0, red_cards_away=1,
        )
        self.assertTrue(check_condition_b(match(away=1, minute=79), stats))
        self.assertTrue(check_condition_b(match(away=1, minute=80), stats))
