"""Minute-adjusted football anomaly signals with a two-group quorum.

Quality thresholds are fixed xG/big-chance advantages. Cumulative shot and
pressure thresholds scale with elapsed time. Missing scraped metrics default
to zero and cannot independently create a quality or pressure signal.
"""

from scraper import LiveMatch, MatchStats

QUORUM_A = 2
QUORUM_B = 2
PRESSURE_THRESHOLD_A = 8.0
PRESSURE_ADVANTAGE_A = 3.0


def _pct_ratio_more(a: float, b: float) -> float:
    if b == 0:
        return 999.0 if a > 0 else 0.0
    return ((a - b) / b) * 100.0


def _minute_scale(minute: int) -> float:
    """Scale cumulative counts around minute 60, clamped to [0.5, 1.4]."""
    return max(0.5, min(1.4, minute / 60.0))


def _condition_a_selection(match: LiveMatch, stats: MatchStats) -> tuple[str, list[str]]:
    if match.score_home != match.score_away:
        return "unknown", []

    scale = _minute_scale(match.minute)
    signals: dict[str, list[str]] = {"home": [], "away": []}
    for side, other, team in (
        ("home", "away", match.home_team),
        ("away", "home", match.away_team),
    ):
        def value(metric: str, which: str = side) -> float:
            return getattr(stats, f"{metric}_{which}")

        def advantage(metric: str) -> float:
            return value(metric) - value(metric, other)

        rules = signals[side]
        # Possession-supported shots and extreme volume share one vote: they
        # measure the same pressure and must not manufacture a quorum alone.
        possession_volume = (
            advantage("possession") >= 20
            and value("total_shots") >= max(6, 8 * scale)
            and advantage("total_shots") >= 4 * scale
        )
        extreme_volume = (
            value("total_shots") >= max(6, 9 * scale)
            and advantage("total_shots") >= 6 * scale
        )
        if possession_volume or extreme_volume:
            rules.append(
                f"Şut baskısı ({team}): {value('total_shots')}-{value('total_shots', other)} şut, "
                f"%{value('possession'):.0f} topa sahip olma"
            )

        if advantage("shots_on_target") >= 3 * scale:
            rules.append(
                f"İsabetli şut üstünlüğü ({team}): "
                f"{value('shots_on_target')}-{value('shots_on_target', other)} "
                f"(+{advantage('shots_on_target'):g})"
            )

        # Correlated chance-quality metrics contribute one independent group.
        if advantage("expected_goals") >= 1.0 or advantage("big_chances") >= 2:
            rules.append(
                f"Fırsat kalitesi ({team}): xG +{advantage('expected_goals'):.2f}, "
                f"büyük fırsat +{advantage('big_chances'):g}"
            )

        pressure = value("dangerous_attacks") / 10 + value("corner_kicks")
        other_pressure = value("dangerous_attacks", other) / 10 + value("corner_kicks", other)
        if (
            pressure >= PRESSURE_THRESHOLD_A * scale
            and pressure - other_pressure >= PRESSURE_ADVANTAGE_A * scale
        ):
            rules.append(
                f"Sürekli baskı endeksi ({team}): {pressure:.1f}-{other_pressure:.1f} "
                f"(tehlikeli atak / 10 + korner)"
            )

        if value("red_cards", other) > value("red_cards"):
            rules.append(f"Kırmızı kart: rakip eksik oyuncu — {team} lehine")

    home, away = signals["home"], signals["away"]
    if len(home) == len(away):
        return "unknown", []
    side = "home" if len(home) > len(away) else "away"
    return (side, signals[side]) if len(signals[side]) >= QUORUM_A else ("unknown", [])


def condition_a_dominant_side(match: LiveMatch, stats: MatchStats) -> str:
    """Persist the team actually supported by the emitted draw signals."""
    return _condition_a_selection(match, stats)[0]


def check_condition_a(match: LiveMatch, stats: MatchStats) -> list[str]:
    """Tied score with at least two signal groups supporting the same team."""
    return _condition_a_selection(match, stats)[1]


def check_condition_b(match: LiveMatch, stats: MatchStats) -> list[str]:
    """One-goal deficit with quality/volume and supporting pressure signals."""
    if abs(match.score_home - match.score_away) != 1:
        return []

    losing = "home" if match.score_home < match.score_away else "away"
    winning = "away" if losing == "home" else "home"
    losing_team = match.home_team if losing == "home" else match.away_team
    winning_team = match.away_team if winning == "away" else match.home_team

    def value(metric: str, side: str = losing) -> float:
        return getattr(stats, f"{metric}_{side}")

    scale = _minute_scale(match.minute)
    late_game = match.minute >= 80
    triggered: list[str] = []
    ts_pct = _pct_ratio_more(value("total_shots"), value("total_shots", winning))
    sot_pct = _pct_ratio_more(value("shots_on_target"), value("shots_on_target", winning))
    threshold_pct = 70 if late_game else 50
    volume_quality = (
        value("total_shots") >= max(6, int(8 * scale))
        and value("shots_on_target") >= max(3, int(4 * scale))
        and ts_pct >= threshold_pct and sot_pct >= threshold_pct
    )
    xg_diff = value("expected_goals") - value("expected_goals", winning)
    chances_diff = value("big_chances") - value("big_chances", winning)
    if xg_diff >= 1.5 or chances_diff >= 3:
        triggered.append(
            f"Kaybeden takım ({losing_team}) yüksek fırsat kalitesi: "
            f"xG +{xg_diff:.2f}, büyük fırsat +{chances_diff:g}"
        )
    elif volume_quality:
        triggered.append(
            f"Kaybeden takım ({losing_team}) şut üstün: "
            f"{value('total_shots')}-{value('total_shots', winning)} toplam, "
            f"{value('shots_on_target')}-{value('shots_on_target', winning)} isabetli"
        )

    # Cards form one vulnerability group, avoiding two votes for discipline.
    if value("red_cards", winning) >= 1:
        triggered.append(f"Kazanan takım ({winning_team}) kırmızı kart gördü — eksik oyuncu")
    elif value("yellow_cards", winning) >= 4:
        triggered.append(
            f"Savunma kırılganlığı: kazanan takım ({winning_team}) "
            f"{value('yellow_cards', winning)} sarı kart gördü"
        )

    if not late_game:
        l_da, w_da = value("dangerous_attacks"), value("dangerous_attacks", winning)
        if l_da >= max(15, int(20 * scale)) and _pct_ratio_more(l_da, w_da) >= 40:
            triggered.append(
                f"Kaybeden takım ({losing_team}) tehlikeli atak üstün: {l_da}-{w_da}"
            )

    return triggered if len(triggered) >= QUORUM_B else []


def detect_anomalies(match: LiveMatch, stats: MatchStats) -> list[tuple[str, list[str]]]:
    """Preserve the worker/Telegram (condition_type, rules) contract."""
    results = []
    for condition, check in (("A", check_condition_a), ("B", check_condition_b)):
        rules = check(match, stats)
        if rules:
            results.append((condition, rules))
    return results
