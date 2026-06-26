"""
cogni.agent.gate_learn
======================
Learn rules from NETLIST-GATE SURPRISES.

The `ready` gate checks RTL with Verilator, then synthesizes with Yosys. When
the netlist gate finds a hazard (an inferred latch cell, a multidriven net)
*while the RTL gate was clean*, that is a SURPRISE: the rulebook -- via
Verilator -- failed to predict a real gate-level problem.

This is exactly when a new rule should be born. We mint a tool-gap rule that
records the gap ("Verilator-clean does not imply synth-clean for <hazard>"),
predicting the SYNTH measurement so it is gradeable, and promote it into the
master pack. Next run, the RTL predictor recalls it and can anticipate the
hazard *before* paying for synthesis.

Deterministic and pure except for `learn_from_surprises`, which appends to a
session `kb_edits.jsonl` and calls `promote_session` (dedup + guards + atomic
write all reused). No LLM required.
"""
from __future__ import annotations

import json
import os

# Map a synth hazard key back to the RTL-lint key Verilator reports clean.
_RTL_LINT_KEY = {
    "latch": "rtl.lint.latch.count",
    "multidriven": "rtl.lint.multidriven.count",
    "width": "rtl.lint.width.count",
}


def netlist_surprises(rtl_blockers, net_blockers) -> list[dict]:
    """A surprise = a netlist hazard found while the RTL gate was CLEAN.

    If RTL itself flagged blockers, the netlist finding is no surprise (the
    rulebook already knew) -- only learn when Verilator passed but synthesis
    did not.
    """
    if rtl_blockers:
        return []
    out = []
    for b in net_blockers:
        out.append({
            "measurement_key": b.measurement_key,           # synth.warnings.latch
            "measured": b.measured,
            "hazard": str(b.measurement_key).split(".")[-1],  # latch
        })
    return out


def surprise_to_new_rule(s: dict, *, design: str) -> dict:
    """Mint a gradeable tool-gap rule from one surprise. Scoped to Verilator at
    the RTL stage; predicts the SYNTH measurement (horizon=synth) so it never
    creates a false RTL blocker -- it only commits at the synthesis boundary,
    where it can be confirmed or weakened by the normal grading machinery."""
    key = s["measurement_key"]
    haz = s["hazard"]
    try:
        measured = int(s["measured"])
    except (TypeError, ValueError):
        measured = 1
    rtl_key = _RTL_LINT_KEY.get(haz, f"rtl.lint.{haz}.count")
    return {
        "id": f"r_rtl_synth_{haz}_gap_when_verilator_clean",
        "statement": (
            f"A clean Verilator lint ({rtl_key}=0) does NOT guarantee a "
            f"{haz}-free netlist: Yosys synthesis inferred {measured} {haz} "
            f"cell(s) on RTL that Verilator linted clean (first seen on "
            f"'{design}'). Treat a clean RTL lint as necessary-but-not-"
            f"sufficient and confirm {haz} at synthesis."),
        "when": [{"op": "tag", "name": "rtl_stage"},
                 {"op": "tag", "name": "tool_verilator"}],
        "unless": [],
        "stage": "rtl",
        "strength": "medium",     # a learned caution, not a law
        "predicts": [{
            "measurement_key": key, "channel": "intervals",
            "value": {"min": 1, "max": max(1, measured)}, "horizon": "synth",
        }],
        "prevents": [{
            "downstream_stage": "gate", "downstream_key": key,
            "mechanism": f"{haz} inferred at synthesis despite a clean RTL lint",
        }],
        "rationale": ("Learned from a netlist-gate surprise in `ready`: the RTL "
                      "gate passed but real Yosys synthesis produced the hazard."),
    }


def learn_from_surprises(surprises: list[dict], *, session_dir: str,
                         pack_path: str, design: str, today: str = "") -> dict:
    """Write one `add` kb_edit per surprise, then promote into the master pack.

    Reuses `promote.promote_session`, so dedup (a repeat of the same lesson
    merges, no bloat), the self-defeating-tool-gate guard, and the atomic write
    all apply. Returns {'learned': n, 'promote': <plan/summary>}.
    """
    if not surprises:
        return {"learned": 0, "promote": None}
    os.makedirs(session_dir, exist_ok=True)
    edits_path = os.path.join(session_dir, "kb_edits.jsonl")
    with open(edits_path, "a", encoding="utf-8") as f:
        for s in surprises:
            nr = surprise_to_new_rule(s, design=design)
            f.write(json.dumps({"kind": "add", "new_rule": nr,
                                "scenario": design,
                                "rationale": nr["rationale"]}) + "\n")
    from agent.promote import promote_session
    res = promote_session(session_dir, pack_path, apply=True, today=today)
    return {"learned": len(surprises), "promote": res}
