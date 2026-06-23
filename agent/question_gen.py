"""
cogni.agent.question_gen
========================
Auto-generate gradable questions from a WorldModel + rule pack.

A question is emitted only where THREE things line up (see the catalog):
  1. a design FEATURE is present      (gate on the reader's facts)
  2. a measurable CONSEQUENCE exists  (a Verilator lint measurement key)
  3. some RULE predicts it            (the key appears in a rule's `predicts`)

Crucially, the generator reads fact *values* (counts/structure — relevance)
and measurement *key names* (what is checkable) — but NEVER the measurement
*values* (the answers). The answer stays sealed in the oracle, so the
blind-guess invariant holds: questions are generated before — and without
seeing — the reality they will be graded against.

The output dicts match the hand-written questions.json shape, so the prepare
stage treats generated and hand-written questions identically.
"""
from __future__ import annotations

from typing import Any, Callable


# Catalog: (id, measurement key, fact gate, question template).
# The gate decides relevance from structural facts only.
_RTL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "latch_count",
        "key": "rtl.lint.latch.count",
        "gate": lambda f: _num(f, "rtl.always_comb_blocks") > 0,
        "q": "How many distinct latches will Verilator infer in {top}?",
    },
    {
        "id": "case_incomplete",
        "key": "rtl.lint.case_incomplete.count",
        "gate": lambda f: _num(f, "rtl.case_blocks") > 0,
        "q": "How many always_comb case statements in {top} lack a default "
             "branch and therefore drive a latch on the assigned signal?",
    },
    {
        "id": "blkseq",
        "key": "rtl.lint.blkseq.count",
        "gate": lambda f: _num(f, "rtl.always_ff_blocks") > 0,
        "q": "How many blocking-assignment-inside-always_ff hazards will "
             "Verilator flag in {top}?",
    },
    {
        "id": "width",
        "key": "rtl.lint.width.count",
        "gate": lambda f: _num(f, "rtl.operator_max_bitwidth") > 0
                          or _num(f, "rtl.always_ff_blocks") > 0,
        "q": "How many width-mismatch (WIDTH) lint warnings will Verilator "
             "emit for {top}?",
    },
    {
        "id": "fsm_no_default",
        "key": "rtl.lint.fsm_no_default.count",
        "gate": lambda f: _num(f, "rtl.fsms") > 0,
        "q": "How many FSM case statements in {top} lack a default state "
             "branch?",
    },
]


def _num(facts: dict, key: str) -> float:
    v = facts.get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _predicted_keys(pack: Any) -> set[str]:
    """Set of measurement keys that at least one rule predicts."""
    rules = pack.get("rules", pack) if isinstance(pack, dict) else pack
    if isinstance(rules, dict):
        rules = list(rules.values())
    keys: set[str] = set()
    for r in rules or []:
        for p in (r.get("predicts") or []):
            k = p.get("measurement_key")
            if k:
                keys.add(k)
    return keys


def generate_questions(world, pack: Any = None, *, stage: str = "rtl") -> list[dict]:
    """Return a list of question dicts (questions.json shape) for `world`.

    If `pack` is given, only emit questions whose measurement key some rule
    predicts (Basis 3) — so every generated question is a fair test of the
    rulebook. Without a pack, emit every fact-relevant question.
    """
    if stage != "rtl":
        return []   # only the RTL catalog exists today
    facts = {k: v.value for k, v in world.facts.items()}
    top = facts.get("rtl.module.top") or "the design"
    predicted = _predicted_keys(pack) if pack is not None else None

    out: list[dict] = []
    for entry in _RTL_CATALOG:
        gate: Callable = entry["gate"]
        if not gate(facts):
            continue
        if predicted is not None and entry["key"] not in predicted:
            continue
        out.append({
            "id": f"gen_{entry['id']}",
            "stage": stage,
            "question": entry["q"].format(top=top),
            "verdict": {
                "type": "numeric",
                "measurement_key": entry["key"],
                "tolerance": 0,
            },
            "auto_generated": True,
        })
    return out
