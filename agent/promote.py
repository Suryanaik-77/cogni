"""
agent.promote
=============
Promote what a run LEARNED back into the master rulebook.

Every run loads the pristine pack (packs/rtl/rules.json) and writes its KB
edits into the SESSION folder only (kb_edits.jsonl / <scen>_kb_after.json).
So learning is per-run and thrown away: the next run starts blind and
re-invents the same rule with a fresh id. This module merges a session's
edits back into the master pack so the NEXT run actually reuses them.

Design (safe by default):

  * dry-run unless apply=True; on apply the pack is backed up to <pack>.bak
  * dedup: never add a rule whose id, statement, or (predicts-keys + stage)
    already exist in the pack — so re-learning the same lesson doesn't
    pile up duplicates and ids stay stable.
  * quality gate: by default SKIP "ungradeable" new rules — ones with an
    empty `predicts` (no measurement_key). Those are tool-schema / plumbing
    notes, not functional design rules; per DV review the rulebook holds
    functional rules that can be checked against reality, not lint-key memos.
    Pass include_ungradeable=True to promote them anyway.
  * scope / strengthen / weaken / retire edits are applied to the named
    target rule (deduping added_unless tags).
  * provenance: every change appends a `history` entry naming the source run.

Edits with no effect (duplicate, ungradeable, missing target) are reported,
not applied, so a human can see exactly what was and wasn't promoted.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Similarity helpers (for statement-level dedup)
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9_.]+")


def _norm_tokens(s: str) -> set[str]:
    return set(_WORD.findall((s or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _norm_band(v: Any) -> tuple:
    """Canonicalize a predicts `value` (the band/expectation) into a hashable,
    order-stable form so two predictions are 'equal' iff they assert the same
    thing — independent of dict-vs-list spelling. Forms seen in the wild:
        {"min":0,"max":0}  ·  [lo,hi]  ·  scalar  ·  enum/map dict
    """
    if isinstance(v, dict):
        if "min" in v or "max" in v or "lo" in v or "hi" in v:
            return ("range", v.get("min", v.get("lo")), v.get("max", v.get("hi")))
        return ("map", tuple(sorted((str(k), _norm_band(val)) for k, val in v.items())))
    if isinstance(v, (list, tuple)):
        # A two-number [lo, hi] is an interval — the verdict engine reads it the
        # same as {min,max}, so normalize both to the same form (bool excluded:
        # it's an int subclass but never a bound).
        if len(v) == 2 and all(isinstance(x, (int, float))
                               and not isinstance(x, bool) for x in v):
            return ("range", v[0], v[1])
        return ("seq", tuple(_norm_band(x) for x in v))
    return ("scalar", v)


def _predicts_signature(rule: dict) -> tuple:
    """A rule's gradeable fingerprint: WHAT it predicts, not just which keys.

    Includes measurement_key + channel + horizon + the normalized band/value,
    plus the rule's stage. Two rules share a signature only when they assert
    the SAME measurement(s) at the SAME value — so `latch.count=[0,0]` and
    `latch.count=[1,3]` are distinct lessons and neither suppresses the other.
    Empty when the rule predicts nothing (ungradeable)."""
    items = sorted(
        (p.get("measurement_key", ""), p.get("channel", ""),
         p.get("horizon", ""), _norm_band(p.get("value")))
        for p in (rule.get("predicts") or [])
        if p.get("measurement_key")
    )
    stage = rule.get("applies_to", {}).get("stage") or rule.get("stage")
    if isinstance(stage, list):
        stage = tuple(sorted(stage))
    return (tuple(items), stage)


# ---------------------------------------------------------------------------
# Convert a session new_rule dict into master-pack rule shape
# ---------------------------------------------------------------------------

def _is_self_defeating_tool_unless(u: Any) -> bool:
    """An `unless` that disables the rule for the very tool that grades it.

    The perceiver always tags the producing tool (e.g. tool_verilator), so any
    of these forms silently kills a functional rule on every run with that tool:
      * the string  "tool_verilator"
      * a tag pred  {"op":"tag","name":"tool_verilator"}
      * a tool pred {"op":"tool","name":"verilator"}
    This is the functional-vs-structural corruption — drop them on every path
    (both `add` of a new rule and `scope` of an existing one)."""
    if isinstance(u, str):
        return u.startswith("tool_")
    if isinstance(u, dict):
        if u.get("op") == "tool":
            return True
        if u.get("op") == "tag" and str(u.get("name", "")).startswith("tool_"):
            return True
    return False


def _to_pack_rule(nr: dict, *, session_id: str, today: str) -> dict:
    stage = nr.get("stage")
    stages = stage if isinstance(stage, list) else ([stage] if stage else [])
    unless = [u for u in (nr.get("unless") or [])
              if not _is_self_defeating_tool_unless(u)]
    return {
        "id": nr.get("id"),
        "version": 1,
        "statement": nr.get("statement", ""),
        "kind": nr.get("kind", "tendency"),
        "strength": _canon_strength(nr.get("strength", "medium")),
        "status": nr.get("status", "active"),
        "applies_to": {
            "stage": stages,
            "tools": [],
            "pdks": [],
            "design_class": ["any"],
            "code_origin": ["any"],
        },
        "when": nr.get("when", []),
        "unless": unless,
        "predicts": nr.get("predicts", []),
        "prevents": nr.get("prevents", []),
        "rationale": nr.get("rationale", ""),
        "citations": nr.get("citations", []),
        "authored_by": f"promoted:{session_id}",
        "authored_at": today,
        "history": [{
            "event": "promoted",
            "at": today,
            "from_session": session_id,
            "rationale": nr.get("rationale", ""),
        }],
        "examples": nr.get("examples", {}),
    }


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class Action:
    op: str            # add | scope | strengthen | weaken | retire | rewrite
    rule_id: str
    decision: str      # apply | skip
    detail: str = ""
    payload: dict = field(default_factory=dict)


def _gradeable(rule: dict) -> bool:
    return bool([p for p in (rule.get("predicts") or [])
                 if p.get("measurement_key")])


def _find_duplicate(new_rule: dict, existing: list[dict],
                    *, sim_threshold: float = 0.80) -> str | None:
    """Return the id of an existing rule this one duplicates, else None.

    Dedup is keyed on WHAT a rule predicts, not how it's worded:

      * gradeable rule  -> duplicate only of another rule with the SAME
        predicts-signature (key+channel+horizon+band+stage). Wording is NOT
        used: two rules can read alike yet predict different values, and
        dropping one loses a real, checkable lesson.
      * ungradeable note -> nothing structural to compare, so fall back to
        near-identical wording, but only against OTHER ungradeable notes — a
        plumbing memo must never suppress a gradeable functional rule.
    """
    new_id = new_rule.get("id")
    new_tokens = _norm_tokens(new_rule.get("statement", ""))
    new_sig = _predicts_signature(new_rule)
    new_gradeable = bool(new_sig[0])
    for r in existing:
        if r.get("id") == new_id:
            return r["id"]
        if new_gradeable:
            if _predicts_signature(r) == new_sig:
                return r["id"]
        else:
            if not _predicts_signature(r)[0] and \
               _jaccard(new_tokens, _norm_tokens(r.get("statement", ""))) >= sim_threshold:
                return r["id"]
    return None


def plan(session_dir: str, pack: dict, *, scenario: str | None = None,
         include_ungradeable: bool = False, today: str = "") -> list[Action]:
    """Compute the promotion plan from a session's kb_edits.jsonl."""
    session_id = os.path.basename(os.path.normpath(session_dir))
    edits_path = os.path.join(session_dir, "kb_edits.jsonl")
    actions: list[Action] = []
    if not os.path.exists(edits_path):
        return actions

    existing = list(pack.get("rules", []))
    by_id = {r["id"]: r for r in existing}
    # New rules already queued for add this run, so two identical adds in the
    # same session dedup against each other too.
    pending_adds: list[dict] = []

    with open(edits_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            if scenario and e.get("scenario") != scenario:
                continue
            kind = e.get("kind")

            if kind == "add" and e.get("new_rule"):
                pr = _to_pack_rule(e["new_rule"], session_id=session_id, today=today)
                dup = _find_duplicate(pr, existing + pending_adds)
                if dup:
                    actions.append(Action("add", pr["id"], "skip",
                                          f"duplicate of {dup}", pr))
                    continue
                if not _gradeable(pr) and not include_ungradeable:
                    actions.append(Action("add", pr["id"], "skip",
                                          "ungradeable (empty predicts) — "
                                          "plumbing note, not a functional rule", pr))
                    continue
                pending_adds.append(pr)
                actions.append(Action("add", pr["id"], "apply",
                                      "new functional rule" if _gradeable(pr)
                                      else "new (ungradeable, forced)", pr))

            elif kind in ("scope", "strengthen", "weaken", "retire", "rewrite"):
                tid = e.get("target_rule_id")
                if not tid or tid not in by_id:
                    actions.append(Action(kind, tid or "?", "skip",
                                          "target rule not in pack", e))
                    continue
                if kind == "scope":
                    new_unless = [u for u in (e.get("added_unless") or [])
                                  if u not in (by_id[tid].get("unless") or [])]
                    # Refuse self-defeating per-tool gates (see
                    # _is_self_defeating_tool_unless): an `unless` on the
                    # grading tool silently kills the functional rule on every
                    # run. Same guard the `add` path applies.
                    tool_gates = [u for u in new_unless
                                  if _is_self_defeating_tool_unless(u)]
                    new_unless = [u for u in new_unless if u not in tool_gates]
                    if tool_gates and not new_unless:
                        actions.append(Action(kind, tid, "skip",
                                              f"refused self-defeating per-tool "
                                              f"unless {tool_gates} (would disable "
                                              f"the rule for the grading tool)", e))
                        continue
                    if not new_unless:
                        actions.append(Action(kind, tid, "skip",
                                              "unless tags already present", e))
                        continue
                    detail = f"add unless {new_unless}"
                    if tool_gates:
                        detail += f" (dropped self-defeating {tool_gates})"
                    actions.append(Action(kind, tid, "apply",
                                          detail,
                                          {**e, "_new_unless": new_unless}))
                else:
                    actions.append(Action(kind, tid, "apply",
                                          e.get("rationale", ""), e))

    return actions


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

# Strength ladder. The master pack vocabulary is high/medium/low (see
# packs/rtl/rules.json + kb._V1_STRENGTH_MAP). LLM-authored rules sometimes use
# the internal strong/tendency/heuristic/weak names instead. Normalize either
# spelling onto one ladder and always emit the PACK vocabulary, so:
#   * strengthen/weaken actually moves a high/medium rule (the old tables keyed
#     on strong/tendency and silently no-op'd every high/medium rule), and
#   * we never write a word the loader can't map (the old _STRENGTH_DOWN could
#     emit "weak"/"tendency", which kb._V1_STRENGTH_MAP doesn't know).
_STRENGTH_LADDER = ["low", "medium", "high"]
_STRENGTH_SYNONYM = {
    "high": "high", "medium": "medium", "low": "low",
    "strong": "high", "tendency": "medium", "heuristic": "low", "weak": "low",
}


def _canon_strength(s: str) -> str:
    return _STRENGTH_SYNONYM.get(str(s or "").lower(), "medium")


def _step_strength(current: str, *, up: bool) -> str:
    i = _STRENGTH_LADDER.index(_canon_strength(current))
    i = min(i + 1, len(_STRENGTH_LADDER) - 1) if up else max(i - 1, 0)
    return _STRENGTH_LADDER[i]


def apply_plan(pack: dict, actions: list[Action], *, session_id: str,
               today: str) -> dict:
    """Mutate `pack` in place per the apply-decisions. Returns a summary dict."""
    by_id = {r["id"]: r for r in pack.get("rules", [])}
    added = scoped = strengthened = weakened = retired = 0

    for a in actions:
        if a.decision != "apply":
            continue
        if a.op == "add":
            pack["rules"].append(a.payload)
            by_id[a.payload["id"]] = a.payload
            added += 1
        elif a.op == "scope":
            r = by_id[a.rule_id]
            r.setdefault("unless", [])
            r["unless"].extend(a.payload["_new_unless"])
            r.setdefault("history", []).append({
                "event": "scoped", "at": today, "from_session": session_id,
                "rationale": a.detail})
            scoped += 1
        elif a.op in ("strengthen", "weaken"):
            r = by_id[a.rule_id]
            r["strength"] = _step_strength(r.get("strength", "medium"),
                                           up=(a.op == "strengthen"))
            r.setdefault("history", []).append({
                "event": a.op, "at": today, "from_session": session_id,
                "rationale": a.payload.get("rationale", "")})
            if a.op == "strengthen":
                strengthened += 1
            else:
                weakened += 1
        elif a.op == "retire":
            r = by_id[a.rule_id]
            r["status"] = "retired"
            r.setdefault("history", []).append({
                "event": "retired", "at": today, "from_session": session_id,
                "rationale": a.payload.get("rationale", "")})
            retired += 1

    return {"added": added, "scoped": scoped, "strengthened": strengthened,
            "weakened": weakened, "retired": retired}


def promote_session(session_dir: str, pack_path: str, *, apply: bool = False,
                    scenario: str | None = None,
                    include_ungradeable: bool = False,
                    today: str = "") -> dict:
    """Promote a session's learning into the master pack.

    Returns a dict with the plan (always) and what was written (when apply).
    """
    with open(pack_path, encoding="utf-8") as f:
        pack = json.load(f)
    session_id = os.path.basename(os.path.normpath(session_dir))

    actions = plan(session_dir, pack, scenario=scenario,
                   include_ungradeable=include_ungradeable, today=today)

    result: dict[str, Any] = {
        "session": session_id,
        "pack_path": pack_path,
        "applied": False,
        "plan": [{"op": a.op, "rule_id": a.rule_id, "decision": a.decision,
                  "detail": a.detail} for a in actions],
    }

    if apply and any(a.decision == "apply" for a in actions):
        backup = pack_path + ".bak"
        shutil.copy2(pack_path, backup)
        summary = apply_plan(pack, actions, session_id=session_id, today=today)
        _atomic_write_json(pack_path, pack)
        result["applied"] = True
        result["backup"] = backup
        result["summary"] = summary

    return result


def _atomic_write_json(path: str, obj: Any) -> None:
    """Serialize `obj` to `path` atomically.

    The old `open(path, "w")` truncates the master pack BEFORE writing — a crash
    or disk-full mid-serialize leaves a half-written (corrupt) rulebook with only
    the `.bak` to recover from. Instead render fully to a temp file in the same
    directory, fsync it, then `os.replace()` — an atomic rename on the same
    filesystem — so the pack is only ever the old bytes or the complete new ones.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".rules.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
