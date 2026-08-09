"""Pure helpers for fixing a signal's predicted side and grading its result."""

from typing import Any, Mapping


VALID_SIDES = {"home", "away"}


def _number(stats: Mapping[str, Any], key: str) -> float:
    try:
        return float(stats.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _share_delta(home: float, away: float) -> float:
    """Return a scale-independent home advantage in the -1..1 range."""
    total = abs(home) + abs(away)
    return (home - away) / total if total else 0.0


def infer_dominant_side(
    condition_type: str,
    score_home: int,
    score_away: int,
    stats: Mapping[str, Any] | None,
) -> str:
    """Infer which side the anomaly signal says is superior.

    Condition B is explicitly a trailing-team pressure signal, so the team
    behind on the scoreboard is the selection.  For tied Condition A signals,
    a quality-first composite of the same statistics used by the detector is
    used.  The returned side is persisted when the signal is first created.
    """
    if condition_type == "B" and score_home != score_away:
        return "away" if score_home > score_away else "home"

    values = stats or {}
    weighted_metrics = (
        ("shots_on_target", 4.0),
        ("expected_goals", 3.0),
        ("big_chances", 2.5),
        ("total_shots", 2.0),
        ("dangerous_attacks", 1.0),
        ("corner_kicks", 0.8),
        ("possession", 0.75),
    )
    score = 0.0
    for prefix, weight in weighted_metrics:
        score += weight * _share_delta(
            _number(values, f"{prefix}_home"),
            _number(values, f"{prefix}_away"),
        )

    # A red card weakens the receiving side, hence away reds favour home.
    score += 3.0 * (
        _number(values, "red_cards_away")
        - _number(values, "red_cards_home")
    )

    if score > 0:
        return "home"
    if score < 0:
        return "away"
    return "unknown"


def evaluate_signal_result(
    dominant_side: str,
    final_score_home: int,
    final_score_away: int,
) -> str:
    """Grade a win bet on the signal's superior team."""
    if dominant_side not in VALID_SIDES:
        return "unresolved"
    dominant_won = (
        dominant_side == "home" and final_score_home > final_score_away
    ) or (
        dominant_side == "away" and final_score_away > final_score_home
    )
    return "successful" if dominant_won else "failed"
