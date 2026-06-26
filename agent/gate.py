"""
cogni.agent.gate
================
RTL -> gate-level readiness.

The deterministic sweep (agent/sweep.py) tells us which rules a design
violates. This module turns that into a single **GO / NO-GO readiness
verdict** and groups every blocker by the DOWNSTREAM failure it would cause at
(or before) gate-level -- read from each rule's `prevents[*].downstream_stage`.

A *blocker* is a violated rule. Clean RTL (no violations) is GO -- it is safe
to hand to synthesis. This is the "readies our RTL for gate-level" gate.

Pure and deterministic: no LLM, no tools. The orchestrator
(run_real.cmd_ready) feeds it sweep violations and drives the auto-fix loop;
everything here is unit-testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Human labels for the downstream stages a `prevents` entry can name. Anything
# not listed falls back to its raw key.
_STAGE_LABEL = {
    "synth": "synthesis / gate-level netlist",
    "sta":   "static timing (gate-level)",
    "sim":   "gate-level simulation",
    "gate":  "gate-level",
    "pnr":   "place & route",
    "rtl":   "rtl",
}

# Order downstream groups by how late/expensive the failure is to discover.
_STAGE_ORDER = {"synth": 0, "sta": 1, "pnr": 2, "gate": 3, "sim": 4, "rtl": 9}


def stage_label(stage: str) -> str:
    return _STAGE_LABEL.get(stage, stage or "downstream")


@dataclass
class Blocker:
    """One violated rule, with the downstream impact(s) it would cause."""
    rule_id: str
    statement: str
    kind: str
    strength: str
    measurement_key: str
    measured: Any
    expected: Any
    reason: str
    downstream: list[dict] = field(default_factory=list)  # raw prevents entries

    @property
    def downstream_stages(self) -> list[str]:
        seen: list[str] = []
        for d in self.downstream:
            s = d.get("downstream_stage") or d.get("stage")
            if s and s not in seen:
                seen.append(s)
        return seen


def blocker_from_violation(v) -> Blocker:
    """Build a Blocker from a sweep RuleCheck. Uses the first failing check for
    the headline measurement, falling back to the first check."""
    c = None
    for ch in (getattr(v, "checks", None) or []):
        if getattr(ch, "status", "") == "violation":
            c = ch
            break
    if c is None:
        chk = getattr(v, "checks", None) or []
        c = chk[0] if chk else None
    return Blocker(
        rule_id=getattr(v, "rule_id", ""),
        statement=getattr(v, "statement", ""),
        kind=getattr(v, "kind", ""),
        strength=getattr(v, "strength", ""),
        measurement_key=getattr(c, "measurement_key", "") if c else "",
        measured=getattr(c, "measured", None) if c else None,
        expected=getattr(c, "expected", None) if c else None,
        reason=getattr(c, "reason", "") if c else "",
        downstream=list(getattr(v, "prevents", None) or []),
    )


def classify(violations) -> list[Blocker]:
    return [blocker_from_violation(v) for v in violations]


# Universal post-synthesis invariants -- true for ANY design and checked on the
# REAL gate-level netlist (not RTL lint). A nonzero count means the hazard
# survived synthesis into actual cells; this is what RTL lint cannot prove.
NETLIST_INVARIANTS = [
    {"key": "synth.warnings.latch", "max": 0,
     "label": "inferred latch cells in the netlist",
     "mechanism": "a latch reached the gate-level netlist -> CDC/STA hazard and "
                  "hold-time risk; fix the incomplete combinational assignment in RTL"},
    {"key": "synth.warnings.multidriven", "max": 0,
     "label": "multidriven nets in the netlist",
     "mechanism": "multiple drivers on one net -> contention / undefined "
                  "gate-level behavior; give each net a single driver in RTL"},
]


def netlist_blockers(meas: dict) -> list[Blocker]:
    """Blockers from the synthesized netlist's measurements (from
    adapters.synth.yosys.runner.from_rtl). Independent of any chip-specific
    pack -- these invariants hold for every design."""
    out: list[Blocker] = []
    for inv in NETLIST_INVARIANTS:
        v = meas.get(inv["key"])
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > inv["max"]:
            out.append(Blocker(
                rule_id="netlist:" + inv["key"],
                statement=inv["label"], kind="constraint", strength="high",
                measurement_key=inv["key"], measured=v,
                expected={"min": 0, "max": inv["max"]}, reason=inv["label"],
                downstream=[{"downstream_stage": "gate", "mechanism": inv["mechanism"]}],
            ))
    return out


def blocker_to_rulecheck(b: Blocker):
    """Adapt a Blocker back into a sweep.RuleCheck so the fixer (which consumes
    RuleChecks) can propose an RTL edit for a netlist-only hazard."""
    from agent.sweep import RuleCheck, PredictionCheck
    return RuleCheck(
        rule_id=b.rule_id, statement=b.statement, kind=b.kind or "constraint",
        strength=b.strength or "high", stage="rtl", status="violation",
        checks=[PredictionCheck(
            measurement_key=b.measurement_key, channel="intervals",
            expected=b.expected, measured=b.measured, status="violation",
            reason=b.reason)],
        prevents=list(b.downstream),
    )


def verdict(blockers) -> str:
    """GO when nothing blocks gate-level, else NO-GO."""
    return "GO" if not blockers else "NO-GO"


def group_by_downstream(blockers) -> dict[str, list[Blocker]]:
    """{downstream_stage: [Blocker, ...]}. A blocker with several `prevents`
    entries appears under each stage. Blockers with no downstream mapping are
    filed under 'rtl' (still a blocker, just no named downstream)."""
    groups: dict[str, list[Blocker]] = {}
    for b in blockers:
        for s in (b.downstream_stages or ["rtl"]):
            groups.setdefault(s, []).append(b)
    return dict(sorted(groups.items(),
                       key=lambda kv: _STAGE_ORDER.get(kv[0], 8)))


def _mechanism(b: Blocker, stage: str) -> str:
    for d in b.downstream:
        if (d.get("downstream_stage") or d.get("stage")) == stage:
            return (d.get("mechanism") or d.get("downstream_key") or "").strip()
    return ""


def _short(s: str, n: int = 90) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _fmt_expected(exp: Any) -> str:
    """Render a predicts band for humans: {min:0,max:0} -> '0', {min:0,max:3}
    -> '0..3'; lists/scalars as-is."""
    if isinstance(exp, dict) and ("min" in exp or "max" in exp):
        lo, hi = exp.get("min"), exp.get("max")
        if lo == hi and lo is not None:
            return str(lo)
        return f"{'' if lo is None else lo}..{'' if hi is None else hi}"
    if isinstance(exp, (list, tuple)):
        return ", ".join(str(x) for x in exp)
    return str(exp)


def format_readiness(blockers, *, lint: dict | None = None,
                     round_no: int | None = None,
                     max_rounds: int | None = None,
                     title: str = "RTL -> GATE-LEVEL READINESS") -> str:
    """Human-readable GO/NO-GO block with the must-fix list grouped by the
    downstream stage each blocker would break."""
    head = f"=== {title} ==="
    if round_no is not None:
        suffix = f" (after round {round_no}" + (f"/{max_rounds}" if max_rounds else "") + ")"
        head += suffix
    lines = [head]
    v = verdict(blockers)
    if v == "GO":
        lines.append("VERDICT: GO  -- no gate-level blockers; safe to synthesize.")
        if lint is not None:
            lines.append(f"  lint: {lint or '{} (clean)'}")
        return "\n".join(lines)

    lines.append(f"VERDICT: NO-GO  ({len(blockers)} blocker"
                 f"{'s' if len(blockers) != 1 else ''} must be fixed before gate-level)")
    for stage, bs in group_by_downstream(blockers).items():
        lines.append("")
        lines.append(f"BLOCKS {stage_label(stage)}:")
        for b in bs:
            meas = f"{b.measurement_key} = {b.measured}" if b.measurement_key else b.reason
            exp = f" (expected {_fmt_expected(b.expected)})" if b.expected not in (None, "") else ""
            lines.append(f"  [{b.rule_id}] {meas}{exp}")
            mech = _mechanism(b, stage)
            if mech:
                lines.append(f"      -> {_short(mech)}")
    if lint is not None:
        lines.append("")
        lines.append(f"  lint now: {lint or '{}'}")
    return "\n".join(lines)
