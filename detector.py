"""
Anomaly detection engine (v2).

Design principles:
  * QUORUM — at least 2 *independent* signal groups must agree before
    an alert fires. Previously a single weak rule (e.g. 15% possession
    edge) could trigger an alert; correlated stats would also stack as
    if they were independent votes.
  * MINUTE NORMALIZATION — thresholds scale with elapsed minute. A team
    with 8 shots at min 30 is genuinely dominant; at min 80 it is not.
  * DIRECTIONAL RED CARDS — a red card disadvantages the receiving team,
    so the rule now names who is at 10 and who benefits.
  * SHOT QUALITY OVER VOLUME — shots-on-target weighted higher than raw
    shot volume (closest proxy to xG in this data source).
  * BASELINE TRAILING BEHAVIOR EXCLUDED — in Condition B, possession or
    dangerous-attacks edge alone is normal "chasing the game" behavior,
    not an anomaly. Harder evidence required.
  * LATE-GAME DAMPING — Condition B after min 80 raises thresholds
    because late pressure is often desperation noise, not signal.
"""

from scraper import LiveMatch, MatchStats

# Minimum number of independent signals needed to emit an alert.
QUORUM_A = 2
QUORUM_B = 2


def _pct_ratio_more(a: float, b: float) -> float:
    """Percentage by which `a` exceeds `b`. E.g. a=13, b=10 → 30%.

    When b == 0 and a > 0, return a large sentinel (999) so the ratio always
    beats any percentage threshold without being silently capped.
    """
    if b == 0:
        return 999.0 if a > 0 else 0.0
    return ((a - b) / b) * 100.0


def _minute_scale(minute: int) -> float:
    """Scale factor on absolute-count thresholds given elapsed minutes.

    Match minute 60 is the baseline (scale = 1.0). Earlier minutes scale
    down (fewer cumulative events expected), later minutes scale up.
    Clamped to [0.5, 1.4] so very early/late outliers don't blow up the
    thresholds or make them trivial.
    """
    raw = minute / 60.0
    return max(0.5, min(1.4, raw))


def check_condition_a(match: LiveMatch, stats: MatchStats) -> list[str]:
    """Condition A — tied score + quorum of independent dominance signals."""
    if match.score_home != match.score_away:
        return []

    scale = _minute_scale(match.minute)
    triggered: list[str] = []

    ts_home, ts_away = stats.total_shots_home, stats.total_shots_away
    sot_home, sot_away = stats.shots_on_target_home, stats.shots_on_target_away

    # ── Signal 1 — possession AND shot-volume dominance on the SAME side ──
    # Possession alone is not a valid signal (park-the-bus / counter-attack
    # tactics), so we require it to co-occur with a same-side shot edge.
    poss_diff = abs(stats.possession_home - stats.possession_away)
    if poss_diff >= 20:
        dom_is_home = stats.possession_home > stats.possession_away
        dom_ts = ts_home if dom_is_home else ts_away
        other_ts = ts_away if dom_is_home else ts_home
        min_total = max(6, int(8 * scale))
        ts_pct = _pct_ratio_more(dom_ts, other_ts)
        if dom_ts >= min_total and ts_pct >= 40:
            dominant = "Ev sahibi" if dom_is_home else "Deplasman"
            triggered.append(
                f"Topa sahip olma + şut baskısı: "
                f"{stats.possession_home:.0f}%-{stats.possession_away:.0f}%, "
                f"{ts_home}-{ts_away} şut ({dominant} üstün)"
            )

    # ── Signal 2 — shots-on-target dominance (best xG proxy in-source) ──
    min_sot = max(3, int(4 * scale))
    dom_sot = max(sot_home, sot_away)
    if dom_sot >= min_sot:
        sot_pct = _pct_ratio_more(dom_sot, min(sot_home, sot_away))
        if sot_pct >= 75:
            dominant = "Ev sahibi" if sot_home > sot_away else "Deplasman"
            triggered.append(
                f"İsabetli şut üstünlüğü: {sot_home}-{sot_away} "
                f"({dominant} +{sot_pct:.0f}%)"
            )

    # ── Signal 3 — shot-volume dominance (large gap) ──
    min_ts = max(6, int(9 * scale))
    dom_ts = max(ts_home, ts_away)
    if dom_ts >= min_ts:
        ts_pct = _pct_ratio_more(dom_ts, min(ts_home, ts_away))
        if ts_pct >= 70:
            dominant = "Ev sahibi" if ts_home > ts_away else "Deplasman"
            triggered.append(
                f"Toplam şut üstünlüğü: {ts_home}-{ts_away} "
                f"({dominant} +{ts_pct:.0f}%)"
            )

    # ── Signal 4 — directional red card (only if one side is down) ──
    r_home, r_away = stats.red_cards_home, stats.red_cards_away
    if r_home > r_away:
        triggered.append(
            f"Kırmızı kart: {match.home_team} eksik oyuncu — "
            f"{match.away_team} lehine"
        )
    elif r_away > r_home:
        triggered.append(
            f"Kırmızı kart: {match.away_team} eksik oyuncu — "
            f"{match.home_team} lehine"
        )

    if len(triggered) < QUORUM_A:
        return []
    return triggered


def check_condition_b(match: LiveMatch, stats: MatchStats) -> list[str]:
    """Condition B — 1-goal difference + quorum of pressure signals.

    Only the losing team's *excess* pressure counts — trailing teams
    naturally chase, so baseline possession/attacks edge is not a signal.
    """
    score_diff = abs(match.score_home - match.score_away)
    if score_diff != 1:
        return []

    late_game = match.minute >= 80

    if match.score_home > match.score_away:
        losing_team = match.away_team
        winning_team = match.home_team
        l_da = stats.dangerous_attacks_away
        w_da = stats.dangerous_attacks_home
        l_ts = stats.total_shots_away
        w_ts = stats.total_shots_home
        l_sot = stats.shots_on_target_away
        w_sot = stats.shots_on_target_home
        w_red = stats.red_cards_home
    else:
        losing_team = match.home_team
        winning_team = match.away_team
        l_da = stats.dangerous_attacks_home
        w_da = stats.dangerous_attacks_away
        l_ts = stats.total_shots_home
        w_ts = stats.total_shots_away
        l_sot = stats.shots_on_target_home
        w_sot = stats.shots_on_target_away
        w_red = stats.red_cards_away

    scale = _minute_scale(match.minute)
    triggered: list[str] = []

    # ── Signal 1 — losing team has BOTH volume and quality shot dominance ──
    min_total = max(6, int(8 * scale))
    min_sot = max(3, int(4 * scale))
    ts_pct = _pct_ratio_more(l_ts, w_ts)
    sot_pct = _pct_ratio_more(l_sot, w_sot)
    # Late-game threshold is stricter (desperation noise filter).
    threshold_pct = 70 if late_game else 50
    if (
        l_ts >= min_total and l_sot >= min_sot
        and ts_pct >= threshold_pct and sot_pct >= threshold_pct
    ):
        triggered.append(
            f"Kaybeden takım ({losing_team}) şut üstün: "
            f"{l_ts}-{w_ts} toplam (+{ts_pct:.0f}%), "
            f"{l_sot}-{w_sot} isabetli (+{sot_pct:.0f}%)"
        )

    # ── Signal 2 — directional red card against winner ──
    if w_red >= 1:
        triggered.append(
            f"Kazanan takım ({winning_team}) kırmızı kart gördü — eksik oyuncu"
        )

    # ── Signal 3 — dangerous-attacks edge (secondary; skipped late-game) ──
    if not late_game:
        min_da = max(15, int(20 * scale))
        da_pct = _pct_ratio_more(l_da, w_da)
        if l_da >= min_da and da_pct >= 40:
            triggered.append(
                f"Kaybeden takım ({losing_team}) tehlikeli atak üstün: "
                f"{l_da}-{w_da} (+{da_pct:.0f}%)"
            )

    if len(triggered) < QUORUM_B:
        return []
    return triggered


def detect_anomalies(match: LiveMatch, stats: MatchStats) -> list[tuple[str, list[str]]]:
    """Run all conditions. Returns list of (condition_type, triggered_rules)."""
    results = []

    rules_a = check_condition_a(match, stats)
    if rules_a:
        results.append(("A", rules_a))

    rules_b = check_condition_b(match, stats)
    if rules_b:
        results.append(("B", rules_b))

    return results
