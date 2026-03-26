"""
Anomaly detection engine.
Evaluates Condition A (draw) and Condition B (1-goal difference)
against live match statistics.
"""

from scraper import LiveMatch, MatchStats


def _pct_more(a: float, b: float) -> float:
    """Return how much `a` is percentage-wise more than `b`. E.g. a=60, b=40 → 20."""
    return a - b


def _pct_ratio_more(a: float, b: float) -> float:
    """Return percentage by which `a` exceeds `b`. E.g. a=13, b=10 → 30%."""
    if b == 0:
        return 100.0 if a > 0 else 0.0
    return ((a - b) / b) * 100.0


def check_condition_a(match: LiveMatch, stats: MatchStats) -> list[str]:
    """
    Condition A: Score is TIED (Draw).
    Returns list of triggered rule descriptions.
    """
    if match.score_home != match.score_away:
        return []

    triggered = []

    # 1. Possession: one team >= 15% higher
    possession_diff = abs(stats.possession_home - stats.possession_away)
    if possession_diff >= 15:
        dominant = "Ev sahibi" if stats.possession_home > stats.possession_away else "Deplasman"
        triggered.append(
            f"Topa sahip olma farkı: {stats.possession_home:.0f}% vs {stats.possession_away:.0f}% "
            f"({dominant} +{possession_diff:.0f}%)"
        )

    # 2. Dangerous Attacks: one team >= 30% more with minimum 15 for dominant
    da_home, da_away = stats.dangerous_attacks_home, stats.dangerous_attacks_away
    if max(da_home, da_away) >= 15:
        da_diff = _pct_ratio_more(max(da_home, da_away), min(da_home, da_away))
        if da_diff >= 30:
            dominant = "Ev sahibi" if da_home > da_away else "Deplasman"
            triggered.append(
                f"Tehlikeli ataklar: {da_home} vs {da_away} "
                f"({dominant} +{da_diff:.0f}%)"
            )

    # 3. Total Shots: one team >= 50% more with minimum 5 for dominant
    ts_home, ts_away = stats.total_shots_home, stats.total_shots_away
    if max(ts_home, ts_away) >= 5:
        ts_diff = _pct_ratio_more(max(ts_home, ts_away), min(ts_home, ts_away))
        if ts_diff >= 50:
            dominant = "Ev sahibi" if ts_home > ts_away else "Deplasman"
            triggered.append(
                f"Toplam şutlar: {ts_home} vs {ts_away} "
                f"({dominant} +{ts_diff:.0f}%)"
            )

    # 4. Shots on Target: one team >= 50% more with minimum 3 for dominant
    sot_home, sot_away = stats.shots_on_target_home, stats.shots_on_target_away
    if max(sot_home, sot_away) >= 3:
        sot_diff = _pct_ratio_more(max(sot_home, sot_away), min(sot_home, sot_away))
        if sot_diff >= 50:
            dominant = "Ev sahibi" if sot_home > sot_away else "Deplasman"
            triggered.append(
                f"İsabetli şutlar: {sot_home} vs {sot_away} "
                f"({dominant} +{sot_diff:.0f}%)"
            )

    # 5. Cards: >= 3 Yellow total OR >= 1 Red total
    total_yellow = stats.yellow_cards_home + stats.yellow_cards_away
    total_red = stats.red_cards_home + stats.red_cards_away
    if total_yellow >= 3 or total_red >= 1:
        triggered.append(
            f"Kartlar: {total_yellow} Sarı, {total_red} Kırmızı (toplam)"
        )

    return triggered


def check_condition_b(match: LiveMatch, stats: MatchStats) -> list[str]:
    """
    Condition B: Score difference is EXACTLY 1.
    Analyzes the LOSING team stats.
    Returns list of triggered rule descriptions.
    """
    score_diff = abs(match.score_home - match.score_away)
    if score_diff != 1:
        return []

    # Determine winning/losing side
    if match.score_home > match.score_away:
        winning_side = "Home"
        losing_side = "Away"
        # Losing team stats
        l_poss = stats.possession_away
        w_poss = stats.possession_home
        l_da = stats.dangerous_attacks_away
        w_da = stats.dangerous_attacks_home
        l_ts = stats.total_shots_away
        w_ts = stats.total_shots_home
        l_sot = stats.shots_on_target_away
        w_sot = stats.shots_on_target_home
        w_yellow = stats.yellow_cards_home
        w_red = stats.red_cards_home
    else:
        winning_side = "Away"
        losing_side = "Home"
        l_poss = stats.possession_home
        w_poss = stats.possession_away
        l_da = stats.dangerous_attacks_home
        w_da = stats.dangerous_attacks_away
        l_ts = stats.total_shots_home
        w_ts = stats.total_shots_away
        l_sot = stats.shots_on_target_home
        w_sot = stats.shots_on_target_away
        w_yellow = stats.yellow_cards_away
        w_red = stats.red_cards_away

    losing_team = match.away_team if winning_side == "Home" else match.home_team

    triggered = []

    # 1. Losing team possession > Winning team by at least 5%
    if l_poss > w_poss and (l_poss - w_poss) >= 5:
        triggered.append(
            f"Kaybeden takım ({losing_team}) topa daha çok sahip: "
            f"{l_poss:.0f}% vs {w_poss:.0f}%"
        )

    # 2. Losing team dangerous attacks > Winning team, minimum 10 for losing team
    if l_da > w_da and l_da >= 10:
        triggered.append(
            f"Kaybeden takım ({losing_team}) daha fazla tehlikeli atak: "
            f"{l_da} vs {w_da}"
        )

    # 3. Losing team >= 30% more total shots AND >= 30% more shots on target
    ts_pct = _pct_ratio_more(l_ts, w_ts) if w_ts > 0 else (100.0 if l_ts > 0 else 0.0)
    sot_pct = _pct_ratio_more(l_sot, w_sot) if w_sot > 0 else (100.0 if l_sot > 0 else 0.0)
    if l_ts >= 4 and l_sot >= 2 and ts_pct >= 30 and sot_pct >= 30:
        triggered.append(
            f"Kaybeden takım ({losing_team}) şut üstünlüğü: "
            f"Toplam {l_ts} vs {w_ts} (+{ts_pct:.0f}%), "
            f"İsabetli {l_sot} vs {w_sot} (+{sot_pct:.0f}%)"
        )

    # 4. Winning team has > 2 Yellow OR >= 1 Red
    if w_yellow > 2 or w_red >= 1:
        triggered.append(
            f"Kazanan takım kartları: {w_yellow} Sarı, {w_red} Kırmızı"
        )

    return triggered


def detect_anomalies(match: LiveMatch, stats: MatchStats) -> list[tuple[str, list[str]]]:
    """
    Run all conditions. Returns list of (condition_type, triggered_rules) tuples.
    """
    results = []

    rules_a = check_condition_a(match, stats)
    if rules_a:
        results.append(("A", rules_a))

    rules_b = check_condition_b(match, stats)
    if rules_b:
        results.append(("B", rules_b))

    return results
