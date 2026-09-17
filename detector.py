"""Football anomaly detection: evidence-group based, minute-aware.

Design (RULE_VERSION below):
 * Every correlated attacking metric belongs to exactly one evidence group —
   VOLUME, QUALITY, TERRITORIAL_PRESSURE, NUMERICAL_ADVANTAGE, or
   RECENT_PRESSURE — so a single underlying dominance never casts multiple
   votes toward quorum.
 * A rule's metric must actually be present in the (possibly partial)
   statistics snapshot. A missing value is never compared against an
   implicit zero, and can never manufacture dominance.
 * Condition B requires a genuine ATTACKING_CORE for the trailing team
   (xG, big-chance, or convincing shot-volume superiority) before any
   supporting evidence (net red cards, corners, dangerous attacks, recent
   pressure) is even considered.
 * Yellow cards are contextual only — they can never complete a quorum.
 * Red cards are directional and net: only a real numerical advantage
   (the opponent has strictly more men sent off) counts as evidence.
 * Dangerous attacks are a secondary, provider-noisy signal. They never form
   their own composite score and can only support another group.
 * Minute-scaled thresholds change smoothly. There is no discontinuity at
   minute 80 — Condition B's volume/quality ratio requirement ramps
   linearly between minutes 70 and 90 instead of jumping in one tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from scraper import LiveMatch, MatchStats

# Bumped whenever the evidence-group logic or its thresholds change.
# Every persisted signal stores the version that produced it so historical
# decisions stay interpretable after future rule changes.
RULE_VERSION = "2026-09-v3"

QUORUM_A = 2
QUORUM_B = 2

SIDES = ("home", "away")


def _minute_scale(minute: int) -> float:
    """Scale cumulative-count thresholds around minute 60, clamped to [0.5, 1.4]."""
    return max(0.5, min(1.4, minute / 60.0))


def _late_ramp(minute: int, start: int = 70, end: int = 90) -> float:
    """Smooth 0..1 ramp between ``start`` and ``end`` minutes.

    Replaces a hard cliff (e.g. 50% -> 70% in a single tick at minute 80)
    with a linear interpolation, so no single minute sees a discontinuous
    rule change.
    """
    if minute <= start:
        return 0.0
    if minute >= end:
        return 1.0
    return (minute - start) / (end - start)


def _other(side: str) -> str:
    return "away" if side == "home" else "home"


def _val(stats: MatchStats, metric: str, side: str) -> Optional[float]:
    return getattr(stats, f"{metric}_{side}", None)


def _pair(stats: MatchStats, metric: str, side: str):
    return _val(stats, metric, side), _val(stats, metric, _other(side))


def _advantage(stats: MatchStats, metric: str, side: str) -> Optional[float]:
    """Return ``side``'s advantage over the other side, or None if either is missing."""
    a, b = _pair(stats, metric, side)
    if a is None or b is None:
        return None
    return a - b


def _effective_total_shots(stats: MatchStats, side: str):
    """Return provider totals, or nullable on/off-target totals as fallback."""
    total_side, total_other = _pair(stats, "total_shots", side)
    if total_side is not None and total_other is not None:
        return total_side, total_other

    on_side, on_other = _pair(stats, "shots_on_target", side)
    off_side, off_other = _pair(stats, "shots_off_target", side)
    if None in (on_side, on_other, off_side, off_other):
        return None, None
    return on_side + off_side, on_other + off_other


@dataclass
class RecentPressure:
    """Optional last-N-minute deltas, supplied by the caller from observation
    history. A missing side/metric entry means "not enough history yet" and
    can never manufacture a signal — it is treated exactly like a missing
    statistic.
    """

    delta_shots_on_target: dict = field(default_factory=dict)
    delta_big_chances: dict = field(default_factory=dict)
    window_minutes: float = 0.0

    def advantage(self, metric: str, side: str) -> Optional[float]:
        table = getattr(self, f"delta_{metric}", None)
        if not table:
            return None
        a, b = table.get(side), table.get(_other(side))
        if a is None or b is None:
            return None
        return a - b


# ---------------------------------------------------------------------------
# Condition A — tied score, quorum of independent evidence groups
# ---------------------------------------------------------------------------

def _volume_group(stats: MatchStats, side: str, scale: float) -> Optional[str]:
    """VOLUME — cumulative shot-count dominance, optionally possession-backed."""
    ts_side, ts_other = _effective_total_shots(stats, side)
    if ts_side is None or ts_other is None:
        return None
    adv = ts_side - ts_other

    extreme = ts_side >= max(6, 9 * scale) and adv >= 6 * scale

    possession_adv = _advantage(stats, "possession", side)
    possession_backed = (
        possession_adv is not None and possession_adv >= 20
        and ts_side >= max(6, 8 * scale) and adv >= 4 * scale
    )

    if extreme or possession_backed:
        poss_val = _val(stats, "possession", side)
        poss_txt = f", %{poss_val:.0f} topa sahip olma" if poss_val is not None else ""
        return f"Şut baskısı: {int(ts_side)}-{int(ts_other)} şut{poss_txt}"
    return None


def _quality_group(stats: MatchStats, side: str, scale: float) -> Optional[str]:
    """QUALITY — one vote for chance quality: shots-on-target, xG, or big chances."""
    sot_adv = _advantage(stats, "shots_on_target", side)
    if sot_adv is not None and sot_adv >= 3 * scale:
        sot_side, sot_other = _pair(stats, "shots_on_target", side)
        return f"İsabetli şut üstünlüğü: {int(sot_side)}-{int(sot_other)} (+{sot_adv:g})"

    xg_adv = _advantage(stats, "expected_goals", side)
    bc_adv = _advantage(stats, "big_chances", side)
    if (xg_adv is not None and xg_adv >= 1.0) or (bc_adv is not None and bc_adv >= 2):
        xg_txt = f"xG +{xg_adv:.2f}" if xg_adv is not None else "xG veri yok"
        bc_txt = f"büyük fırsat +{bc_adv:g}" if bc_adv is not None else "büyük fırsat veri yok"
        return f"Fırsat kalitesi: {xg_txt}, {bc_txt}"
    return None


def _territorial_pressure_group(stats: MatchStats, side: str, scale: float) -> Optional[str]:
    """TERRITORIAL_PRESSURE — corner dominance; dangerous attacks only corroborate.

    Dangerous-attack counts are provider-noisy and never form their own
    composite score (no more ``dangerous_attacks / 10 + corners``); they can
    only be mentioned alongside a real corner edge.
    """
    corner_side, corner_other = _pair(stats, "corner_kicks", side)
    if corner_side is None or corner_other is None:
        return None
    adv = corner_side - corner_other
    if corner_side < max(4, int(5 * scale)) or adv < 4 * scale:
        return None

    da_adv = _advantage(stats, "dangerous_attacks", side)
    da_txt = f", tehlikeli atak +{da_adv:g}" if da_adv is not None and da_adv > 0 else ""
    return f"Saha baskısı: {int(corner_side)}-{int(corner_other)} korner{da_txt}"


def _numerical_advantage_group(stats: MatchStats, side: str) -> Optional[str]:
    """NUMERICAL_ADVANTAGE — directional, net red-card advantage only.

    If both sides have red cards, only the net difference counts. A red
    card against the signalled side is never positive evidence for it.
    """
    red_side, red_other = _pair(stats, "red_cards", side)
    if red_side is None or red_other is None:
        return None
    net = red_other - red_side
    if net >= 1:
        return f"Kırmızı kart: rakip eksik oyuncu (net +{net:g})"
    return None


def _recent_pressure_group(recent: Optional[RecentPressure], side: str) -> Optional[str]:
    """RECENT_PRESSURE — a currently-building edge distinct from full-match totals."""
    if recent is None:
        return None
    sot_adv = recent.advantage("shots_on_target", side)
    bc_adv = recent.advantage("big_chances", side)
    if (sot_adv is not None and sot_adv >= 2) or (bc_adv is not None and bc_adv >= 1):
        window = recent.window_minutes or 10
        return f"Son {window:g} dakikada artan baskı"
    return None


_CONDITION_A_GROUPS = (
    ("VOLUME", _volume_group),
    ("QUALITY", _quality_group),
    ("TERRITORIAL_PRESSURE", _territorial_pressure_group),
)


def _condition_a_selection(
    match: LiveMatch, stats: MatchStats, recent: Optional[RecentPressure] = None,
) -> tuple[str, list[str], list[str]]:
    """Return (selected_side, reasons, evidence_groups); ('unknown', [], []) otherwise."""
    if match.score_home != match.score_away:
        return "unknown", [], []

    scale = _minute_scale(match.minute)
    per_side: dict[str, list[tuple[str, str]]] = {"home": [], "away": []}
    for side in SIDES:
        groups: list[tuple[str, str]] = []
        for group_name, fn in _CONDITION_A_GROUPS:
            reason = fn(stats, side, scale)
            if reason:
                groups.append((group_name, reason))
        numeric_reason = _numerical_advantage_group(stats, side)
        if numeric_reason:
            groups.append(("NUMERICAL_ADVANTAGE", numeric_reason))
        recent_reason = _recent_pressure_group(recent, side)
        if recent_reason:
            groups.append(("RECENT_PRESSURE", recent_reason))
        per_side[side] = groups

    home_groups, away_groups = per_side["home"], per_side["away"]
    if len(home_groups) == len(away_groups):
        return "unknown", [], []
    side = "home" if len(home_groups) > len(away_groups) else "away"
    winning_groups = per_side[side]
    if len(winning_groups) < QUORUM_A:
        return "unknown", [], []

    team = match.home_team if side == "home" else match.away_team
    reasons = [f"{reason} ({team})" for _, reason in winning_groups]
    group_names = [name for name, _ in winning_groups]
    return side, reasons, group_names


def condition_a_dominant_side(
    match: LiveMatch, stats: MatchStats, recent: Optional[RecentPressure] = None,
) -> str:
    """Persist the team actually supported by the emitted draw signals."""
    return _condition_a_selection(match, stats, recent)[0]


def check_condition_a(
    match: LiveMatch, stats: MatchStats, recent: Optional[RecentPressure] = None,
) -> list[str]:
    """Tied score with a quorum of independent evidence groups behind one team."""
    return _condition_a_selection(match, stats, recent)[1]


# ---------------------------------------------------------------------------
# Condition B — one-goal deficit, mandatory attacking core + supporting evidence
# ---------------------------------------------------------------------------

def _attacking_core(
    stats: MatchStats, losing: str, winning: str, scale: float, minute: int,
) -> Optional[str]:
    """Return a description of the trailing team's attacking core, or None.

    A genuine core is xG superiority, big-chance superiority, or a
    convincing combination of total-shot and shots-on-target superiority.
    Volume/quality percentages never use a zero-denominator sentinel: when
    the winning side has zero shots to compare against, an absolute-edge
    requirement is used instead of a percentage ratio.
    """
    xg_adv = _advantage(stats, "expected_goals", losing)
    if xg_adv is not None and xg_adv >= 1.5:
        return f"yüksek fırsat kalitesi: xG +{xg_adv:.2f}"

    bc_adv = _advantage(stats, "big_chances", losing)
    if bc_adv is not None and bc_adv >= 3:
        return f"yüksek fırsat kalitesi: büyük fırsat +{bc_adv:g}"

    ts_losing, ts_winning = _effective_total_shots(stats, losing)
    sot_losing, sot_winning = _pair(stats, "shots_on_target", losing)
    if None in (ts_losing, ts_winning, sot_losing, sot_winning):
        return None

    if ts_winning <= 0 or sot_winning <= 0:
        # No meaningful "% more than" ratio can be built off a zero
        # baseline; require an absolute edge instead of a sentinel ratio.
        ts_ok = ts_losing >= max(8, int(10 * scale)) and (ts_losing - ts_winning) >= 6
        sot_ok = sot_losing >= max(4, int(5 * scale)) and (sot_losing - sot_winning) >= 3
    else:
        threshold_pct = 50 + 20 * _late_ramp(minute)  # smooth 50% -> 70% across 70'-90'
        ts_ok = (
            ts_losing >= max(6, int(8 * scale))
            and ((ts_losing - ts_winning) / ts_winning) * 100 >= threshold_pct
        )
        sot_ok = (
            sot_losing >= max(3, int(4 * scale))
            and ((sot_losing - sot_winning) / sot_winning) * 100 >= threshold_pct
        )

    if ts_ok and sot_ok:
        return f"şut üstün: {int(ts_losing)}-{int(ts_winning)} toplam, {int(sot_losing)}-{int(sot_winning)} isabetli"
    return None


def _supporting_evidence(
    stats: MatchStats, losing: str, winning: str,
    losing_team: str, winning_team: str, minute: int,
    recent: Optional[RecentPressure],
) -> list[tuple[str, str]]:
    """Optional evidence that corroborates — but never replaces — the core.

    Yellow cards are deliberately excluded: they are contextual only and
    can never contribute to Condition B's quorum (surfaced separately in
    the stats summary instead).
    """
    support: list[tuple[str, str]] = []

    red_winning = _val(stats, "red_cards", winning)
    red_losing = _val(stats, "red_cards", losing)
    if red_winning is not None and red_losing is not None:
        net = red_winning - red_losing
        if net >= 1:
            support.append((
                "NUMERICAL_ADVANTAGE",
                f"Kazanan takım ({winning_team}) kırmızı kart gördü — "
                f"net eksik oyuncu farkı +{net:g}",
            ))

    corner_adv = _advantage(stats, "corner_kicks", losing)
    if corner_adv is not None and corner_adv >= 3:
        support.append((
            "TERRITORIAL_PRESSURE",
            f"Kaybeden takım ({losing_team}) korner üstünlüğü: +{corner_adv:g}",
        ))

    if minute < 80:
        da_adv = _advantage(stats, "dangerous_attacks", losing)
        if da_adv is not None and da_adv >= 10:
            support.append((
                "TERRITORIAL_PRESSURE",
                f"Kaybeden takım ({losing_team}) tehlikeli atak üstünlüğü: +{da_adv:g}",
            ))

    recent_reason = _recent_pressure_group(recent, losing)
    if recent_reason:
        support.append(("RECENT_PRESSURE", f"Kaybeden takım ({losing_team}): {recent_reason}"))

    return support


def _condition_b_selection(
    match: LiveMatch, stats: MatchStats, recent: Optional[RecentPressure] = None,
) -> tuple[str, list[str], list[str]]:
    """Return (trailing_side, reasons, evidence_groups); ('unknown', [], []) otherwise."""
    if match.score_home == match.score_away:
        return "unknown", [], []
    if abs(match.score_home - match.score_away) != 1:
        return "unknown", [], []

    losing = "home" if match.score_home < match.score_away else "away"
    winning = _other(losing)
    losing_team = match.home_team if losing == "home" else match.away_team
    winning_team = match.home_team if winning == "home" else match.away_team
    scale = _minute_scale(match.minute)

    core_reason = _attacking_core(stats, losing, winning, scale, match.minute)
    if not core_reason:
        return "unknown", [], []

    support = _supporting_evidence(
        stats, losing, winning, losing_team, winning_team, match.minute, recent,
    )
    distinct_groups = {"ATTACKING_CORE"} | {group for group, _ in support}
    if len(distinct_groups) < QUORUM_B:
        return "unknown", [], []

    reasons = [f"Kaybeden takım ({losing_team}) {core_reason}"]
    reasons += [reason for _, reason in support]
    return losing, reasons, sorted(distinct_groups)


def condition_b_dominant_side(
    match: LiveMatch, stats: MatchStats, recent: Optional[RecentPressure] = None,
) -> str:
    """Persist the trailing team the emitted Condition B signal is backing."""
    return _condition_b_selection(match, stats, recent)[0]


def check_condition_b(
    match: LiveMatch, stats: MatchStats, recent: Optional[RecentPressure] = None,
) -> list[str]:
    """One-goal deficit with a mandatory attacking core plus supporting evidence."""
    return _condition_b_selection(match, stats, recent)[1]


# ---------------------------------------------------------------------------
# Combined detection
# ---------------------------------------------------------------------------

@dataclass
class DetectedSignal:
    condition: str
    side: str
    reasons: list[str]
    groups: list[str]


def detect_anomalies_detailed(
    match: LiveMatch, stats: MatchStats, recent: Optional[RecentPressure] = None,
) -> list[DetectedSignal]:
    """Full evidence detail for persistence (first-signal snapshot, groups)."""
    results: list[DetectedSignal] = []

    a_side, a_reasons, a_groups = _condition_a_selection(match, stats, recent)
    if a_reasons:
        results.append(DetectedSignal("A", a_side, a_reasons, a_groups))

    b_side, b_reasons, b_groups = _condition_b_selection(match, stats, recent)
    if b_reasons:
        results.append(DetectedSignal("B", b_side, b_reasons, b_groups))

    return results


def detect_anomalies(
    match: LiveMatch, stats: MatchStats, recent: Optional[RecentPressure] = None,
) -> list[tuple[str, list[str]]]:
    """Preserve the worker/Telegram (condition_type, rules) contract."""
    return [(s.condition, s.reasons) for s in detect_anomalies_detailed(match, stats, recent)]
