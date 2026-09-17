"""Pure helpers for fixing a signal's predicted side and grading its result.

``infer_dominant_side`` remains for callers that create a new signal through the
legacy interface.  Database migrations deliberately do not run it over old
rows: absent historical decision metadata must stay unknown.
"""

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
    the detector's explicit selection is used when available; older snapshots
    fall back to a quality-first statistical composite.  The returned side is persisted when the signal is first created.
    """
    if condition_type == "B" and score_home != score_away:
        return "away" if score_home > score_away else "home"

    values = stats or {}
    if values.get("signal_side") in VALID_SIDES:
        return values["signal_side"]

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


def evaluate_selected_team_outcome(
    selected_side: str | None,
    final_score_home: int,
    final_score_away: int,
) -> dict[str, object | None]:
    """Return the explicit selected-team result contract.

    The old ``successful``/``failed`` value is included for compatibility with
    the existing archive API.  New reporting should use ``outcome`` and
    ``selected_team_won`` so that a draw is not silently conflated with a loss.
    """
    if selected_side not in VALID_SIDES:
        return {
            "outcome": "UNKNOWN",
            "selected_team_won": None,
            "legacy_result_status": "unresolved",
        }

    selected_score, opponent_score = (
        (final_score_home, final_score_away)
        if selected_side == "home"
        else (final_score_away, final_score_home)
    )
    if selected_score > opponent_score:
        outcome = "WON"
    elif selected_score == opponent_score:
        outcome = "DREW"
    else:
        outcome = "LOST"
    return {
        "outcome": outcome,
        "selected_team_won": outcome == "WON",
        "legacy_result_status": "successful" if outcome == "WON" else "failed",
    }


def evaluate_equalization(
    condition_type: str,
    selected_side: str | None,
    signal_score_home: int,
    signal_score_away: int,
    later_scores: list[tuple[int, int]],
) -> bool | None:
    """Return ``True`` only when retained observations prove equalization.

    A final losing score cannot prove that a trailing team never equalized, so
    absence of positive observation evidence remains ``None`` rather than
    ``False``.  ``scored_next`` is intentionally not inferred here because
    polling snapshots cannot reliably establish goal order.
    """
    if condition_type != "B" or selected_side not in VALID_SIDES:
        return None

    selected_signal, opponent_signal = (
        (signal_score_home, signal_score_away)
        if selected_side == "home"
        else (signal_score_away, signal_score_home)
    )
    if selected_signal >= opponent_signal:
        return None

    for home_score, away_score in later_scores:
        selected_score, opponent_score = (
            (home_score, away_score)
            if selected_side == "home"
            else (away_score, home_score)
        )
        if selected_score >= opponent_score:
            return True
    return None
