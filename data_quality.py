"""Exact provider statistic mapping and conservative, nullable normalization."""
from dataclasses import dataclass, field, fields
import math
import re


@dataclass
class MatchStats:
    possession_home: float | None = None
    possession_away: float | None = None
    dangerous_attacks_home: int | None = None
    dangerous_attacks_away: int | None = None
    total_shots_home: int | None = None
    total_shots_away: int | None = None
    shots_on_target_home: int | None = None
    shots_on_target_away: int | None = None
    shots_off_target_home: int | None = None
    shots_off_target_away: int | None = None
    blocked_shots_home: int | None = None
    blocked_shots_away: int | None = None
    shots_inside_box_home: int | None = None
    shots_inside_box_away: int | None = None
    shots_outside_box_home: int | None = None
    shots_outside_box_away: int | None = None
    big_chances_home: int | None = None
    big_chances_away: int | None = None
    expected_goals_home: float | None = None
    expected_goals_away: float | None = None
    yellow_cards_home: int | None = None
    yellow_cards_away: int | None = None
    red_cards_home: int | None = None
    red_cards_away: int | None = None
    offsides_home: int | None = None
    offsides_away: int | None = None
    corner_kicks_home: int | None = None
    corner_kicks_away: int | None = None
    fouls_home: int | None = None
    fouls_away: int | None = None
    accurate_passes_home: int | None = None
    accurate_passes_away: int | None = None
    total_passes_home: int | None = None
    total_passes_away: int | None = None
    pass_accuracy_home: float | None = None
    pass_accuracy_away: float | None = None
    period: str = "ALL"
    fetched_at: float | None = None
    field_status: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    validation_status: str = "PARTIAL"
    validation_errors: list[str] = field(default_factory=list)
    raw_payload: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)
                if f.name.endswith(("_home", "_away"))}

    @property
    def missing_fields(self) -> list[str]:
        return [name for name, value in self.to_dict().items() if value is None]


ALIASES = {
    "possession": ("ballPossession", "possession"),
    "total_shots": ("totalShotsOnGoal", "totalShots", "shotsTotal", "totalAttempts"),
    "shots_on_target": ("shotsOnGoal", "shotsOnTarget", "onTarget"),
    "shots_off_target": ("shotsOffGoal", "shotsOffTarget", "offTarget"),
    "blocked_shots": ("blockedScoringAttempt", "blockedShots", "blockedShot"),
    "shots_inside_box": ("totalShotsInsideBox", "shotsInsideBox"),
    "shots_outside_box": ("totalShotsOutsideBox", "shotsOutsideBox"),
    "expected_goals": ("expectedGoals", "xG"),
    "big_chances": ("bigChanceCreated", "bigChancesCreated", "bigChances", "bigChance"),
    "dangerous_attacks": ("dangerousAttacks", "dangerousAttack"),
    "corner_kicks": ("cornerKicks", "corners"),
    "yellow_cards": ("yellowCards", "yellowCard"),
    "red_cards": ("redCards", "redCard"),
    "offsides": ("offsides", "offside"),
    "fouls": ("fouls", "foul"),
    "accurate_passes": ("accuratePasses", "passesAccurate"),
    "total_passes": ("passes", "totalPasses"),
    "pass_accuracy": ("passAccuracy", "accuratePassesPercentage", "passingAccuracy"),
}


def canonical_key(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


STAT_FIELDS = {canonical_key(alias): name for name, aliases in ALIASES.items()
               for alias in (*aliases, name)}
NUM = r"[-+]?\d+(?:[.,]\d+)?"


def number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif re.fullmatch(NUM, str(value).strip()):
        result = float(str(value).strip().replace(",", "."))
    else:
        return None
    return result if math.isfinite(result) else None


def percent(value, *, dedicated: bool = False) -> float | None:
    if isinstance(value, str):
        match = re.search(r"(" + NUM + r")\s*%", value)
        if match:
            return number(match.group(1))
    return number(value) if dedicated else None


def count(value) -> int | None:
    # Explicit display count formats, never search arbitrary text for a number.
    parsed = number(value)
    if parsed is None and isinstance(value, str):
        match = re.fullmatch(r"\s*(" + NUM + r")\s*\(\s*" + NUM + r"\s*\)\s*", value)
        parsed = number(match.group(1)) if match else None
    return int(parsed) if parsed is not None and parsed.is_integer() else None


def ratio(value) -> tuple[int | None, int | None]:
    if not isinstance(value, str):
        return None, None
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)(?:\s*\(\s*" + NUM + r"\s*%\s*\))?\s*", value)
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def validate_stats(stats: MatchStats) -> MatchStats:
    errors = [f"conflicting aliases: {key}" for key, status in stats.field_status.items()
              if status == "conflict"]
    for key, value in stats.to_dict().items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            errors.append(f"invalid numeric value: {key}")
        elif key.startswith(("possession_", "pass_accuracy_")) and value > 100:
            errors.append(f"percentage outside 0..100: {key}")
    for side in ("home", "away"):
        total = getattr(stats, f"total_shots_{side}")
        for part in ("shots_on_target", "shots_off_target", "blocked_shots", "shots_inside_box", "shots_outside_box"):
            value = getattr(stats, f"{part}_{side}")
            if isinstance(total, (int, float)) and isinstance(value, (int, float)) and value > total:
                errors.append(f"{part}_{side} exceeds total_shots_{side}")
        accurate, passes = getattr(stats, f"accurate_passes_{side}"), getattr(stats, f"total_passes_{side}")
        if accurate is not None and passes is not None and accurate > passes:
            errors.append(f"accurate_passes_{side} exceeds total_passes_{side}")
    # Do not enforce sum(shot components)==total or possession sums: provider
    # definitions and rounding vary. We reject impossible inequalities only.
    stats.validation_errors = sorted(set(errors))
    stats.validation_status = ("INVALID" if errors else
                               "PARTIAL" if stats.missing_fields or stats.period != "ALL" else "VALID")
    return stats


def normalize_statistics(data: dict, fetched_at: float) -> MatchStats:
    stats = MatchStats(fetched_at=fetched_at, raw_payload=data)
    periods = [p for p in data.get("statistics", []) if isinstance(p, dict)]
    all_periods = [p for p in periods if p.get("period") == "ALL"]
    if all_periods:
        chosen = all_periods  # Conflicting repeated ALL fields are detected below.
        stats.period = "ALL"
    elif len(periods) == 1:
        chosen = periods
        raw_period = str(periods[0].get("period", "UNKNOWN")).upper()
        stats.period = raw_period if raw_period in ("1ST", "2ND", "ET") else "UNKNOWN"
    else:
        chosen = []
        stats.period = "UNKNOWN"
    absent = "missing" if stats.period == "ALL" else "period_unavailable"
    stats.field_status = dict.fromkeys(stats.to_dict(), absent)
    candidates: dict[str, list[tuple[float | int | None, str, dict]]] = {}

    def add(key, value, raw, origin, derived=False):
        status = "derived" if derived else "observed" if value is not None else (
            "missing" if raw is None or raw == "" else "parse_error")
        candidates.setdefault(key, []).append((value, status, origin))

    for period in chosen:
        for group in period.get("groups", []):
            for item in group.get("statisticsItems", []):
                key = item.get("key")
                # An unknown nonempty key never falls through to a possibly
                # ambiguous display name. Human labels only work without keys.
                metric = STAT_FIELDS.get(canonical_key(key or item.get("name", "")))
                if metric is None:
                    continue
                for side in ("home", "away"):
                    raw = item.get(f"{side}Value")
                    display = item.get(side)
                    value = raw if raw is not None else display
                    origin = {"key": key, "name": item.get("name"), "raw": raw,
                              "display": display, "period": period.get("period")}
                    field_name = f"{metric}_{side}"
                    if metric == "accurate_passes":
                        numerator, denominator = ratio(display)
                        parsed = count(value)
                        if parsed is None and numerator is not None:
                            parsed = numerator
                        add(field_name, parsed, value, origin)
                        if denominator is not None:
                            add(f"total_passes_{side}", denominator, display, origin, derived=True)
                        explicit_pct = percent(display)
                        if explicit_pct is not None:
                            add(f"pass_accuracy_{side}", explicit_pct, display, origin)
                    elif metric in ("possession", "pass_accuracy"):
                        add(field_name, percent(value, dedicated=True), value, origin)
                    elif metric == "expected_goals":
                        add(field_name, number(value), value, origin)
                    else:
                        add(field_name, count(value), value, origin)
    for name, values in candidates.items():
        present = {v for v, _, _ in values if v is not None}
        bad = any(status == "parse_error" for _, status, _ in values)
        if len(present) > 1 or (present and bad):
            stats.field_status[name] = "conflict"
        elif present:
            setattr(stats, name, next(iter(present)))
            stats.field_status[name] = "observed" if any(s == "observed" for _, s, _ in values) else "derived"
        else:
            stats.field_status[name] = "parse_error" if bad else "missing"
        stats.provenance[name] = [entry for _, _, entry in values]
    for side in ("home", "away"):
        total_key = f"total_shots_{side}"
        components = [getattr(stats, f"{name}_{side}") for name in
                      ("shots_on_target", "shots_off_target", "blocked_shots")]
        if total_key not in candidates and all(v is not None for v in components):
            setattr(stats, total_key, sum(components))
            stats.field_status[total_key] = "derived"
            stats.provenance[total_key] = {"formula": "on_target + off_target + blocked"}
        accuracy_key = f"pass_accuracy_{side}"
        accurate, total = getattr(stats, f"accurate_passes_{side}"), getattr(stats, f"total_passes_{side}")
        if accuracy_key not in candidates and accurate is not None and total is not None and total > 0:
            setattr(stats, accuracy_key, accurate * 100 / total)
            stats.field_status[accuracy_key] = "derived"
            stats.provenance[accuracy_key] = {"formula": "100 * accurate_passes / total_passes"}
    return validate_stats(stats)
