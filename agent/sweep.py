"""
cogni.agent.sweep
=================
Deterministic rule-sweep engine.

Given a knowledge pack and a Reality (oracle output), evaluate every
applicable rule's `predicts[*]` against the measured value and classify:

  - violation : measurement is present and falls outside the predicted band
  - clean     : measurement is present and lies inside the band
  - skipped   : rule's `when`/`unless` blocked it (gating, e.g. missing pdk.band)
  - na        : rule applies, but the measurement key is not in reality

No LLM calls. No predictor, no verifier, no reflection. This is the pure-Python
classifier the CLI runs first; the fix proposer (`agent.fixer`) only sees
violations.

Channel semantics for `predicts[*]`:

  intervals : reality[key] must be a number; PASS iff value.min <= x <= value.max
              (open-ended bounds: omit min/max)
  enum      : reality[key] must equal one of value (list of allowed)
  includes  : reality[key] (string or list) must include each item in value
  excludes  : reality[key] (string or list) must include none of value
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from agent.predicates import evaluate_when_unless


# -----------------------------------------------------------------------------
# Result objects
# -----------------------------------------------------------------------------

@dataclass
class PredictionCheck:
    """One `predicts[*]` entry evaluated against reality."""
    measurement_key: str
    channel: str
    expected: Any
    measured: Any
    status: str          # "violation" | "clean" | "na"
    reason: str = ""     # short human string ("outside band 11-18, got 6.26")


@dataclass
class RuleCheck:
    """One rule evaluated against the world+reality."""
    rule_id: str
    statement: str
    kind: str            # constraint | tendency | heuristic | identity
    strength: str        # high | medium | low
    stage: str | list[str] | None
    status: str          # "violation" | "clean" | "skipped" | "na"
    reason: str = ""     # gating reason if skipped
    checks: list[PredictionCheck] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    examples: dict = field(default_factory=dict)
    prevents: list[dict] = field(default_factory=list)


@dataclass
class SweepReport:
    pack_path: str
    stage: str
    n_rules_total: int
    n_rules_applicable: int
    n_violations: int
    n_clean: int
    n_skipped: int
    n_na: int
    rules: list[RuleCheck]

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def violations(self) -> list[RuleCheck]:
        return [r for r in self.rules if r.status == "violation"]


# -----------------------------------------------------------------------------
# Channel evaluators
# -----------------------------------------------------------------------------

def _check_intervals(measured, expected: dict) -> tuple[str, str]:
    """expected = {min?, max?, unit?}. Numeric measurement only."""
    if measured is None:
        return "na", "measurement absent"
    try:
        x = float(measured)
    except (TypeError, ValueError):
        return "na", f"measurement is not numeric: {measured!r}"
    lo = expected.get("min")
    hi = expected.get("max")
    unit = expected.get("unit", "")
    band = f"[{lo if lo is not None else '-inf'} .. {hi if hi is not None else '+inf'}]"
    if lo is not None and x < lo:
        return "violation", f"measured {x}{unit} below band {band}"
    if hi is not None and x > hi:
        return "violation", f"measured {x}{unit} above band {band}"
    return "clean", f"measured {x}{unit} in band {band}"


def _check_enum(measured, expected: list) -> tuple[str, str]:
    if measured is None:
        return "na", "measurement absent"
    allowed = list(expected) if isinstance(expected, (list, tuple)) else [expected]
    # Case-insensitive substring match for resilience: top_module names often
    # include suffixes (ibex_multdiv_fast vs "multdiv").
    m = str(measured).lower()
    hits = [v for v in allowed if str(v).lower() in m or m in str(v).lower()]
    if hits:
        return "clean", f"measured {measured!r} matches allowed {hits}"
    return "violation", f"measured {measured!r} not in allowed {allowed}"


def _as_list(x) -> list[str]:
    if x is None:
        return []
    if isinstance(x, (list, tuple, set)):
        return [str(i) for i in x]
    return [str(x)]


def _check_includes(measured, expected: list) -> tuple[str, str]:
    if measured is None:
        return "na", "measurement absent"
    haystack = _as_list(measured)
    haystack_blob = " ".join(haystack).lower()
    need = list(expected) if isinstance(expected, (list, tuple)) else [expected]
    missing = [v for v in need if str(v).lower() not in haystack_blob]
    if missing:
        return "violation", f"measured does not include {missing}"
    return "clean", f"measured includes all of {need}"


def _check_excludes(measured, expected: list) -> tuple[str, str]:
    if measured is None:
        return "na", "measurement absent"
    haystack = _as_list(measured)
    haystack_blob = " ".join(haystack).lower()
    forbid = list(expected) if isinstance(expected, (list, tuple)) else [expected]
    found = [v for v in forbid if str(v).lower() in haystack_blob]
    if found:
        return "violation", f"measured contains forbidden tokens {found}"
    return "clean", f"measured contains none of {forbid}"


_CHANNEL_EVAL = {
    "intervals": _check_intervals,
    "enum":      _check_enum,
    "includes":  _check_includes,
    "excludes":  _check_excludes,
}


# -----------------------------------------------------------------------------
# Reality lookup
# -----------------------------------------------------------------------------

def _reality_get(reality, key: str):
    """Reality is either a dataclass-like with .measurements, or a plain dict."""
    if reality is None:
        return None
    m = getattr(reality, "measurements", None)
    if m is None and isinstance(reality, dict):
        m = reality.get("measurements", reality)
    if m is None:
        return None
    return m.get(key)


# -----------------------------------------------------------------------------
# Core sweep
# -----------------------------------------------------------------------------

def _gating_reason(rule: dict, world, reality, target: dict) -> str | None:
    """Return None if rule applies, else short reason for skipping.

    PDK-band gating: any `when` predicate that references a pdk.* tag or
    a pdk.* key is treated as silently skipped when missing — this is the
    user-confirmed behavior (PDK rules opt-in).
    """
    when = rule.get("when") or []
    unless = rule.get("unless") or []

    # Detect pdk gates first so we can produce a friendlier skip reason.
    def _refs_pdk(p: dict) -> bool:
        if not isinstance(p, dict):
            return False
        if p.get("op") == "tag" and "pdk" in str(p.get("name", "")).lower():
            return True
        if "pdk" in str(p.get("key", "")).lower():
            return True
        return False

    if not evaluate_when_unless(when, unless, world, reality, target):
        # Try to attribute the failure to a pdk gate for nicer messaging.
        for p in when:
            if _refs_pdk(p):
                return f"pdk gate not satisfied ({p.get('name') or p.get('key')})"
        return "when/unless gate not satisfied"
    return None


def sweep(pack: dict,
          world,
          reality,
          *,
          stage_filter: str | None = None,
          scenario_target: dict | None = None) -> SweepReport:
    """Run the deterministic sweep.

    Args:
      pack: parsed v1 pack dict (must have a "rules" list).
      world: WorldModel-like (used for predicate evaluation: facts, tags).
      reality: Reality-like (provides .measurements dict).
      stage_filter: if set, only consider rules whose `applies_to.stage`
        contains this value (or equals it).
      scenario_target: optional dict consulted by predicates as a third
        lookup tier (matches existing predicates.evaluate signature).

    Returns SweepReport.
    """
    rules = pack.get("rules", [])
    target = scenario_target or {}

    rule_checks: list[RuleCheck] = []
    n_violations = n_clean = n_skipped = n_na = n_applicable = 0

    for r in rules:
        # ---- retired rules never fire (recall skips them too) ----
        if r.get("status") == "retired":
            continue
        # ---- stage filter ----
        if stage_filter is not None:
            r_stages = (r.get("applies_to") or {}).get("stage", [])
            if isinstance(r_stages, str):
                r_stages = [r_stages]
            if r_stages and stage_filter not in r_stages:
                continue  # not even reported

        rc = RuleCheck(
            rule_id=r["id"],
            statement=r.get("statement", ""),
            kind=r.get("kind", ""),
            strength=r.get("strength", ""),
            stage=(r.get("applies_to") or {}).get("stage"),
            status="na",
            citations=r.get("citations", []),
            examples=r.get("examples", {}),
            prevents=r.get("prevents", []),
        )

        # ---- gate ----
        gate_reason = _gating_reason(r, world, reality, target)
        if gate_reason is not None:
            rc.status = "skipped"
            rc.reason = gate_reason
            rule_checks.append(rc)
            n_skipped += 1
            continue

        n_applicable += 1

        # ---- evaluate each predicts[*] ----
        worst = "clean"   # clean < na < violation in our reporting order
        any_check = False
        for pr in r.get("predicts", []):
            chan = pr.get("channel")
            evf = _CHANNEL_EVAL.get(chan)
            if evf is None:
                rc.checks.append(PredictionCheck(
                    measurement_key=pr.get("measurement_key", ""),
                    channel=chan or "?",
                    expected=pr.get("value"),
                    measured=None,
                    status="na",
                    reason=f"unknown channel {chan!r}",
                ))
                if worst == "clean":
                    worst = "na"
                continue
            key = pr.get("measurement_key", "")
            measured = _reality_get(reality, key)
            status, reason = evf(measured, pr.get("value"))
            rc.checks.append(PredictionCheck(
                measurement_key=key,
                channel=chan,
                expected=pr.get("value"),
                measured=measured,
                status=status,
                reason=reason,
            ))
            any_check = True
            if status == "violation":
                worst = "violation"
            elif status == "na" and worst != "violation":
                worst = "na"

        if not any_check:
            rc.status = "na"
            rc.reason = "rule has no predicts[*]"
            n_na += 1
        else:
            rc.status = worst
            if worst == "violation":
                n_violations += 1
            elif worst == "clean":
                n_clean += 1
            else:
                n_na += 1

        rule_checks.append(rc)

    return SweepReport(
        pack_path=pack.get("__path__", ""),
        stage=stage_filter or "",
        n_rules_total=len(rules),
        n_rules_applicable=n_applicable,
        n_violations=n_violations,
        n_clean=n_clean,
        n_skipped=n_skipped,
        n_na=n_na,
        rules=rule_checks,
    )
