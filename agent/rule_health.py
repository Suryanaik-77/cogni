"""
agent.rule_health
=================
Rule health tracking, LLM-researched diagnosis, and self-correction.

When violations are found, the agent does NOT blindly suggest weakening
rules. Instead it sends each violation to the LLM for research:

  - What RTL design principle does this rule encode?
  - Is this violation a real design bug or a miscalibrated rule?
  - What do industry standards / best practices say?

Only when the LLM concludes (with reasoning) that the rule itself is
wrong does the system suggest a correction. Violations are presumed
to be design bugs unless proven otherwise.

Health data accumulates in ``memory/rule_health.json`` across designs
and runs for long-term tracking.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH_PATH = os.path.join(_REPO_ROOT, "memory", "rule_health.json")

_STRENGTH_LADDER = ["high", "medium", "low"]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Health store — cross-design rule performance tracking
# ---------------------------------------------------------------------------

class RuleHealthStore:
    """Accumulates per-rule health data across designs and runs."""

    def __init__(self, path: str = HEALTH_PATH):
        self._path = path
        if os.path.exists(path):
            with open(path) as f:
                self.data = json.load(f)
        else:
            self.data = {"rules": {}, "corrections": [], "updated_at": _now()}

    def save(self):
        self.data["updated_at"] = _now()
        _atomic_write(self._path, self.data)

    def _ensure(self, rule_id: str) -> dict:
        if rule_id not in self.data["rules"]:
            self.data["rules"][rule_id] = {
                "evaluations": 0,
                "correct": 0,
                "wrong": 0,
                "unfixable": 0,
                "designs_seen": [],
                "observed": [],
                "last_evaluated": None,
            }
        return self.data["rules"][rule_id]

    # ---- recording ----

    def record_evaluation(self, rule_id: str, *, design: str,
                          correct: bool,
                          measurement_key: str = "",
                          measured: Any = None,
                          expected: Any = None) -> None:
        """Record one rule evaluation against reality."""
        r = self._ensure(rule_id)
        r["evaluations"] += 1
        if correct:
            r["correct"] += 1
        else:
            r["wrong"] += 1
        if design and design not in r["designs_seen"]:
            r["designs_seen"].append(design)
        r["last_evaluated"] = _now()
        if measurement_key and measured is not None:
            r["observed"].append({
                "design": design,
                "key": measurement_key,
                "measured": measured,
                "expected": expected,
                "at": _now(),
            })
            r["observed"] = r["observed"][-50:]
        self.save()

    def record_unfixable(self, rule_id: str, *, design: str) -> None:
        """Record that a violation for this rule could not be fixed."""
        r = self._ensure(rule_id)
        r["unfixable"] += 1
        self.save()

    def record_correction(self, correction: dict) -> None:
        self.data.setdefault("corrections", []).append(correction)
        self.save()

    # ---- queries ----

    def accuracy(self, rule_id: str) -> float | None:
        r = self.data["rules"].get(rule_id)
        if not r or r["evaluations"] < 2:
            return None
        total = r["correct"] + r["wrong"]
        return r["correct"] / total if total > 0 else None

    def fix_failure_rate(self, rule_id: str) -> float | None:
        r = self.data["rules"].get(rule_id)
        if not r or r["wrong"] == 0:
            return None
        return r["unfixable"] / r["wrong"] if r["wrong"] > 0 else 0.0

    def observed_range(self, rule_id: str,
                       measurement_key: str = "") -> tuple[float, float] | None:
        r = self.data["rules"].get(rule_id)
        if not r or not r.get("observed"):
            return None
        obs = r["observed"]
        if measurement_key:
            obs = [o for o in obs if o.get("key") == measurement_key]
        values = [o["measured"] for o in obs
                  if isinstance(o.get("measured"), (int, float))]
        if not values:
            return None
        return (min(values), max(values))

    def summary(self, rule_id: str) -> dict | None:
        r = self.data["rules"].get(rule_id)
        if not r:
            return None
        return {
            "evaluations": r["evaluations"],
            "correct": r["correct"],
            "wrong": r["wrong"],
            "unfixable": r["unfixable"],
            "accuracy": self.accuracy(rule_id),
            "designs": len(r["designs_seen"]),
        }


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

@dataclass
class RuleDiagnosis:
    rule_id: str
    issue: str        # band_too_tight | low_accuracy | consistently_unfixable
    severity: str     # critical | warning | info
    detail: str
    recommendation: str  # widen_band | weaken | retire | none
    confidence: str = "low"  # low | medium | high
    measurement_key: str = ""
    old_band: dict | None = None
    new_band: dict | None = None


DIAGNOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["design_bug", "rule_wrong", "rule_too_strict", "inconclusive"],
            "description": "Is this a real design bug, or is the rule itself wrong/miscalibrated?"
        },
        "reasoning": {
            "type": "string",
            "description": "Step-by-step reasoning: what RTL principle does the rule encode, what does the violation mean, and why you reached this verdict."
        },
        "industry_basis": {
            "type": "string",
            "description": "What industry standards, best practices, or synthesis tool behavior supports your conclusion (e.g., IEEE 1800, CDC guidelines, Synopsys/Cadence lint rules)."
        },
        "recommendation": {
            "type": "string",
            "enum": ["no_change", "widen_band", "weaken", "retire", "rewrite_statement"],
            "description": "What should happen to the rule. 'no_change' if the design is wrong."
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "How confident are you in this verdict."
        },
        "suggested_band": {
            "type": ["object", "null"],
            "properties": {"min": {"type": "number"}, "max": {"type": "number"}},
            "description": "If recommendation is widen_band, the new band. Otherwise null."
        },
        "suggested_statement": {
            "type": ["string", "null"],
            "description": "If recommendation is rewrite_statement, the improved statement. Otherwise null."
        }
    },
    "required": ["verdict", "reasoning", "industry_basis", "recommendation", "confidence"]
}


def _build_diagnosis_prompt(rule, violation, health_data):
    """Build an LLM prompt that asks the model to research and decide."""
    obs_history = ""
    if health_data and health_data.get("observed"):
        recent = health_data["observed"][-10:]
        obs_lines = []
        for o in recent:
            obs_lines.append(
                f"  design={o.get('design','?')}: "
                f"measured={o.get('measured')}, expected={o.get('expected')}")
        obs_history = "\n".join(obs_lines)

    designs_seen = health_data.get("designs_seen", []) if health_data else []

    return f"""# Role: RTL Rule Diagnosis Researcher

You are a senior RTL design verification expert. Your job is to determine
whether a rule violation indicates a BUG IN THE DESIGN or a PROBLEM WITH
THE RULE ITSELF.

## IMPORTANT: Default assumption

Violations are DESIGN BUGS unless you have strong evidence the rule is wrong.
Rules encode industry best practices. Do NOT suggest weakening a rule just
because designs violate it -- that would hide real bugs.

## The violated rule

```json
{json.dumps(rule, indent=2, default=str)}
```

## The violation

- Measurement key: {violation.get('measurement_key', '?')}
- Measured value: {violation.get('measured', '?')}
- Expected (rule band): {violation.get('expected', '?')}

## Historical observations across designs

Designs seen: {', '.join(designs_seen) if designs_seen else 'only this one'}
{obs_history if obs_history else '(no prior observations)'}

Evaluations: {health_data.get('evaluations', 0) if health_data else 0}
Correct: {health_data.get('correct', 0) if health_data else 0}
Wrong: {health_data.get('wrong', 0) if health_data else 0}

## Your research task

1. What RTL design principle does this rule encode?
2. What do industry standards (IEEE 1800, CDC whitepapers, synthesis tool
   lint rules from Synopsys/Cadence/Siemens) say about this?
3. Is a measured value of {violation.get('measured', '?')} for
   "{violation.get('measurement_key', '?')}" actually a design problem,
   or is the rule's band unrealistic?
4. Consider: if this is a lint/warning count with band [0,0], zero warnings
   IS the correct target -- widening would hide real bugs.

Give your verdict with full reasoning. Be conservative -- only recommend
changing the rule if you are genuinely confident it is wrong.
"""


def _diagnose_call(rule, violation, health_data, call_name):
    """Create an LLMCall for researching one violation."""
    from agent.llm import LLMCall
    import agent.llm as _llm

    prompt = _build_diagnosis_prompt(rule, violation, health_data)
    return LLMCall(
        name=call_name,
        model=_llm.MODEL_OPUS,
        role="rule_diagnostician",
        prompt=prompt,
        schema=DIAGNOSE_SCHEMA,
        inputs={
            "rule": rule,
            "violation": violation,
            "health_data": health_data or {},
        },
    )


def _write_brief(call, run_dir):
    paths = call.write_brief(run_dir)
    return {
        "name": call.name, "model": call.model, "role": call.role,
        "prompt": paths["prompt"], "schema": paths["schema"],
        "inputs": paths["inputs"], "output": paths["output"],
    }


def _read_output(call, run_dir):
    out = os.path.join(run_dir, "llm_calls", call.name, "output.json")
    if not os.path.exists(out):
        return None
    with open(out, encoding="utf-8") as f:
        return json.load(f)


async def _research_violations(violations_info, run_dir):
    """Send all violations to LLM for research, return list of results."""
    from agent.llm.transports import run_briefs_concurrently

    calls = []
    for i, info in enumerate(violations_info):
        call = _diagnose_call(
            info["rule"], info["violation"], info["health_data"],
            call_name=f"diagnose.{info['rule_id']}_{i}")
        calls.append(call)

    briefs = [_write_brief(c, run_dir) for c in calls]
    if briefs:
        await run_briefs_concurrently(briefs, concurrency=4)

    results = []
    for call, info in zip(calls, violations_info):
        output = _read_output(call, run_dir)
        results.append({
            "rule_id": info["rule_id"],
            "measurement_key": info["violation"].get("measurement_key", ""),
            "output": output,
        })
    return results


def diagnose_remaining(health: RuleHealthStore,
                       pack: dict,
                       remaining_blockers: list,
                       *,
                       design: str = "",
                       run_dir: str = "") -> list[RuleDiagnosis]:
    """Research each violation via LLM to decide: design bug or wrong rule.

    Records health data and sends violations to the LLM for analysis.
    Only returns diagnoses where the LLM concluded the rule is wrong.
    """
    rules_by_id = {r["id"]: r for r in pack.get("rules", [])}
    violations_info = []

    for b in remaining_blockers:
        rule_id = b.rule_id
        rule = rules_by_id.get(rule_id)
        if not rule:
            continue

        mkey = b.measurement_key
        measured = b.measured
        expected = b.expected

        health.record_evaluation(
            rule_id, design=design, correct=False,
            measurement_key=mkey, measured=measured, expected=expected)
        health.record_unfixable(rule_id, design=design)

        r_data = health.data["rules"].get(rule_id, {})

        violations_info.append({
            "rule_id": rule_id,
            "rule": rule,
            "violation": {
                "measurement_key": mkey,
                "measured": measured,
                "expected": expected,
                "design": design,
            },
            "health_data": r_data,
        })

    if not violations_info or not run_dir:
        return []

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                results = pool.submit(
                    asyncio.run,
                    _research_violations(violations_info, run_dir)
                ).result()
        else:
            results = loop.run_until_complete(
                _research_violations(violations_info, run_dir))
    except RuntimeError:
        results = asyncio.run(
            _research_violations(violations_info, run_dir))

    diagnoses: list[RuleDiagnosis] = []
    for res in results:
        output = res.get("output")
        if not output:
            continue

        verdict = output.get("verdict", "inconclusive")
        recommendation = output.get("recommendation", "no_change")

        if verdict == "design_bug" or recommendation == "no_change":
            continue

        reasoning = output.get("reasoning", "")
        industry = output.get("industry_basis", "")
        confidence = output.get("confidence", "low")
        detail = f"{reasoning}\n  Industry basis: {industry}"

        diag_kwargs = dict(
            rule_id=res["rule_id"],
            severity="warning" if confidence != "high" else "critical",
            detail=detail,
            confidence=confidence,
            measurement_key=res.get("measurement_key", ""),
        )

        if recommendation == "widen_band":
            new_band = output.get("suggested_band")
            rule = rules_by_id.get(res["rule_id"], {})
            old_band = None
            for pred in rule.get("predicts", []):
                if pred.get("measurement_key") == res.get("measurement_key"):
                    old_band = dict(pred.get("value", {}))
                    break
            diagnoses.append(RuleDiagnosis(
                issue="band_too_tight",
                recommendation="widen_band",
                old_band=old_band,
                new_band=new_band,
                **diag_kwargs,
            ))
        elif recommendation == "weaken":
            diagnoses.append(RuleDiagnosis(
                issue="rule_too_strong",
                recommendation="weaken",
                **diag_kwargs,
            ))
        elif recommendation == "retire":
            diagnoses.append(RuleDiagnosis(
                issue="rule_wrong",
                recommendation="retire",
                **diag_kwargs,
            ))
        elif recommendation == "rewrite_statement":
            new_stmt = output.get("suggested_statement", "")
            diagnoses.append(RuleDiagnosis(
                issue="rule_unclear",
                recommendation="rewrite",
                detail=f"{detail}\n  Suggested: {new_stmt}",
                **{k: v for k, v in diag_kwargs.items() if k != "detail"},
            ))

    return diagnoses


def record_clean_rules(health: RuleHealthStore,
                       sweep_report,
                       *, design: str = "") -> None:
    """Record correct evaluations for rules that passed (clean).

    Call after sweep to build up the accuracy denominator.
    """
    for rc in sweep_report.rules:
        if rc.status == "clean":
            for check in rc.checks:
                if check.status == "clean":
                    health.record_evaluation(
                        rc.rule_id, design=design, correct=True,
                        measurement_key=check.measurement_key,
                        measured=check.measured, expected=check.expected)


# ---------------------------------------------------------------------------
# Correction — apply fixes to the pack
# ---------------------------------------------------------------------------

def apply_corrections(pack: dict,
                      diagnoses: list[RuleDiagnosis],
                      health: RuleHealthStore) -> list[dict]:
    """Apply diagnosed corrections to the live pack dict.

    Returns list of applied corrections (for logging / memory).
    Modifies pack["rules"] in place so the next sweep uses corrected rules.
    """
    applied: list[dict] = []
    rules_by_id = {r["id"]: r for r in pack.get("rules", [])}

    for d in diagnoses:
        rule = rules_by_id.get(d.rule_id)
        if not rule:
            continue

        correction = {
            "rule_id": d.rule_id,
            "issue": d.issue,
            "recommendation": d.recommendation,
            "detail": d.detail,
            "at": _now(),
        }

        if d.recommendation == "widen_band" and d.new_band and d.measurement_key:
            for pred in rule.get("predicts", []):
                if pred.get("measurement_key") == d.measurement_key:
                    correction["old_value"] = dict(pred.get("value", {}))
                    pred["value"] = d.new_band
                    correction["new_value"] = dict(d.new_band)
                    break
            else:
                continue
            rule.setdefault("history", []).append({
                "at": _now(),
                "event": "auto_corrected",
                "kind": "band_widened",
                "measurement_key": d.measurement_key,
                "old": d.old_band,
                "new": d.new_band,
                "reason": d.detail,
            })

        elif d.recommendation == "weaken":
            old_strength = rule.get("strength", "high")
            idx = _STRENGTH_LADDER.index(old_strength) if old_strength in _STRENGTH_LADDER else 0
            new_strength = _STRENGTH_LADDER[min(idx + 1, len(_STRENGTH_LADDER) - 1)]
            correction["old_strength"] = old_strength
            correction["new_strength"] = new_strength
            rule["strength"] = new_strength
            rule.setdefault("history", []).append({
                "at": _now(),
                "event": "auto_corrected",
                "kind": "weakened",
                "old": old_strength,
                "new": new_strength,
                "reason": d.detail,
            })

        elif d.recommendation == "retire":
            correction["old_status"] = rule.get("status", "active")
            rule["status"] = "retired"
            rule.setdefault("history", []).append({
                "at": _now(),
                "event": "auto_corrected",
                "kind": "retired",
                "reason": d.detail,
            })

        else:
            continue

        applied.append(correction)
        health.record_correction(correction)

    return applied


def persist_pack(pack: dict, pack_path: str) -> None:
    """Write corrected pack back to disk (atomic write)."""
    _atomic_write(pack_path, {k: v for k, v in pack.items() if k != "__path__"})


# ---------------------------------------------------------------------------
# Console formatting
# ---------------------------------------------------------------------------

def format_diagnoses(diagnoses: list[RuleDiagnosis]) -> str:
    if not diagnoses:
        return ""
    lines = ["\n=== RULE HEALTH ANALYSIS (LLM-researched) ==="]
    for d in diagnoses:
        icon = {"critical": "!!", "warning": "!", "info": "~"}.get(d.severity, "?")
        lines.append(
            f"\n  [{icon}] {d.rule_id}  [{d.confidence} confidence]")
        for detail_line in d.detail.split("\n"):
            lines.append(f"      {detail_line.strip()}")
        if d.recommendation == "widen_band" and d.old_band and d.new_band:
            lines.append(
                f"      -> Suggestion: widen band from "
                f"[{d.old_band['min']},{d.old_band['max']}] "
                f"to [{d.new_band['min']},{d.new_band['max']}]")
        elif d.recommendation == "weaken":
            lines.append(f"      -> Suggestion: weaken strength")
        elif d.recommendation == "retire":
            lines.append(f"      -> Suggestion: retire rule")
        elif d.recommendation == "rewrite":
            lines.append(f"      -> Suggestion: rewrite rule statement")
    return "\n".join(lines)


def format_corrections(corrections: list[dict]) -> str:
    if not corrections:
        return ""
    lines = ["[rule-health] applied corrections:"]
    for c in corrections:
        if c.get("new_value"):
            lines.append(
                f"  WIDENED {c['rule_id']}: "
                f"[{c['old_value'].get('min')},{c['old_value'].get('max')}] "
                f"-> [{c['new_value'].get('min')},{c['new_value'].get('max')}]")
        elif c.get("new_strength"):
            lines.append(
                f"  WEAKENED {c['rule_id']}: "
                f"{c['old_strength']} -> {c['new_strength']}")
        elif c.get("recommendation") == "retire":
            lines.append(f"  RETIRED {c['rule_id']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rule inspection helpers (for cmd_rules)
# ---------------------------------------------------------------------------

def pack_summary(pack: dict) -> dict:
    """Aggregate stats about a rule pack."""
    rules = pack.get("rules", [])
    by_status: dict[str, int] = {}
    by_strength: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    mkey_counts: dict[str, int] = {}

    for r in rules:
        st = r.get("status", "active")
        by_status[st] = by_status.get(st, 0) + 1
        by_strength[r.get("strength", "?")] = by_strength.get(r.get("strength", "?"), 0) + 1
        by_kind[r.get("kind", "?")] = by_kind.get(r.get("kind", "?"), 0) + 1
        for s in (r.get("applies_to") or {}).get("stage", []):
            by_stage[s] = by_stage.get(s, 0) + 1
        for p in r.get("predicts", []):
            k = p.get("measurement_key", "")
            if k:
                mkey_counts[k] = mkey_counts.get(k, 0) + 1

    return {
        "total": len(rules),
        "by_status": by_status,
        "by_strength": by_strength,
        "by_stage": by_stage,
        "by_kind": by_kind,
        "top_keys": dict(sorted(mkey_counts.items(),
                                key=lambda x: -x[1])[:15]),
    }


def format_pack_summary(pack: dict, *, pack_path: str = "",
                        health: RuleHealthStore | None = None) -> str:
    """Pretty-print pack summary for cmd_rules."""
    s = pack_summary(pack)
    name = pack.get("pack", os.path.basename(pack_path or "?"))
    version = pack.get("version", "?")
    schema = pack.get("schema", "?")

    lines = [
        f"Rule Pack: {name} (v{version}, {schema})",
        f"  {s['total']} rules total  |  "
        + "  |  ".join(f"{v} {k}" for k, v in sorted(s["by_status"].items())),
        "",
        "  By strength:",
    ]
    for k in ["high", "medium", "low"]:
        if k in s["by_strength"]:
            lines.append(f"    {k:8} : {s['by_strength'][k]}")

    lines += ["", "  By kind:"]
    for k, v in sorted(s["by_kind"].items()):
        lines.append(f"    {k:12} : {v}")

    if s["by_stage"]:
        lines += ["", "  By stage:"]
        for k, v in sorted(s["by_stage"].items()):
            lines.append(f"    {k:8} : {v}")

    if s["top_keys"]:
        lines += ["", "  Top measurement keys:"]
        for k, v in s["top_keys"].items():
            lines.append(f"    {k:40} : {v} rule(s)")

    if health:
        issues = []
        for rule_id, r in health.data.get("rules", {}).items():
            acc = health.accuracy(rule_id)
            if acc is not None and acc < 0.6 and r.get("evaluations", 0) >= 3:
                issues.append(f"    {rule_id}: accuracy {acc:.0%} "
                              f"({r['correct']}/{r['correct']+r['wrong']})")
        if issues:
            lines += ["", "  Health issues:"] + issues

    corrections = health.data.get("corrections", []) if health else []
    if corrections:
        lines += ["", f"  Corrections applied: {len(corrections)}"]
        for c in corrections[-5:]:
            lines.append(f"    [{c.get('recommendation', '?')}] {c['rule_id']} "
                         f"— {c.get('detail', '')[:60]}")

    return "\n".join(lines)


def format_rule_detail(rule: dict, *,
                       health: RuleHealthStore | None = None) -> str:
    """Pretty-print a single rule for inspection."""
    lines = [
        f"Rule: {rule['id']} (v{rule.get('version', '?')})",
        f"  Statement  : {rule.get('statement', '?')}",
        f"  Kind       : {rule.get('kind', '?')}",
        f"  Strength   : {rule.get('strength', '?')}",
        f"  Status     : {rule.get('status', '?')}",
    ]
    stages = (rule.get("applies_to") or {}).get("stage", [])
    if stages:
        lines.append(f"  Stage      : {', '.join(stages) if isinstance(stages, list) else stages}")

    preds = rule.get("predicts", [])
    if preds:
        lines += ["", "  Predicts:"]
        for p in preds:
            val = p.get("value", {})
            if isinstance(val, dict):
                band = f"[{val.get('min', '-inf')}, {val.get('max', '+inf')}]"
            else:
                band = str(val)
            lines.append(
                f"    {p.get('measurement_key', '?')} in {band}  "
                f"(channel={p.get('channel', '?')}, horizon={p.get('horizon', '?')})")

    prevents = rule.get("prevents", [])
    if prevents:
        lines += ["", "  Prevents:"]
        for pv in prevents:
            lines.append(
                f"    {pv.get('downstream_key', '?')} at "
                f"{pv.get('downstream_stage', '?')} stage")

    when = rule.get("when", [])
    if when:
        lines += ["", "  When:"]
        for w in when:
            if isinstance(w, dict):
                lines.append(f"    {w.get('op', '?')}: {w.get('name', w.get('key', '?'))}")
            else:
                lines.append(f"    tag: {w}")

    examples = rule.get("examples", {})
    if examples:
        lines.append("")
        for label in ("violating", "compliant"):
            exs = examples.get(label, [])
            if exs:
                lines.append(f"  {label.title()} example:")
                snippet = exs[0][:120]
                for sl in snippet.split("\n"):
                    lines.append(f"    | {sl}")

    if health:
        s = health.summary(rule['id'])
        if s:
            lines += [
                "",
                "  Health:",
                f"    Evaluated  : {s['evaluations']} times across {s['designs']} design(s)",
                f"    Accuracy   : {s['accuracy']:.0%}" if s['accuracy'] is not None
                    else f"    Accuracy   : n/a (< 2 evaluations)",
                f"    Correct    : {s['correct']}",
                f"    Wrong      : {s['wrong']}",
                f"    Unfixable  : {s['unfixable']}",
            ]

    hist = rule.get("history", [])
    corrections_in_hist = [h for h in hist if h.get("event") == "auto_corrected"]
    if corrections_in_hist:
        lines += ["", "  Auto-corrections:"]
        for h in corrections_in_hist[-5:]:
            lines.append(f"    [{h.get('kind', '?')}] {h.get('reason', '')[:60]}")

    return "\n".join(lines)


def validate_rule(rule: dict, key_index: dict | None = None) -> list[str]:
    """Validate a single rule for structural issues. Returns list of problems."""
    problems: list[str] = []
    if not rule.get("id"):
        problems.append("missing id")
    if not rule.get("statement"):
        problems.append("missing statement")
    if rule.get("kind") not in ("constraint", "tendency", "heuristic", "identity", None, ""):
        problems.append(f"unknown kind: {rule.get('kind')}")
    if rule.get("strength") not in ("high", "medium", "low", None, ""):
        problems.append(f"unknown strength: {rule.get('strength')}")
    if rule.get("status") not in ("active", "shadow", "retired", None, ""):
        problems.append(f"unknown status: {rule.get('status')}")

    for i, pred in enumerate(rule.get("predicts", [])):
        mk = pred.get("measurement_key", "")
        if not mk:
            problems.append(f"predicts[{i}]: missing measurement_key")
        elif key_index and mk not in key_index:
            problems.append(f"predicts[{i}]: undeclared key '{mk}' "
                            f"(not in pack key_index)")
        ch = pred.get("channel", "")
        if ch not in ("intervals", "enum", "ranking", "includes", "excludes", "legacy", ""):
            problems.append(f"predicts[{i}]: unknown channel '{ch}'")
        if ch == "intervals":
            val = pred.get("value", {})
            if not isinstance(val, dict):
                problems.append(f"predicts[{i}]: intervals channel needs dict value")
        if not pred.get("horizon"):
            problems.append(f"predicts[{i}]: missing horizon")

    if not rule.get("predicts"):
        problems.append("no predicts[] entries (ungradeable rule)")

    if not rule.get("examples", {}).get("violating"):
        problems.append("missing violating example")
    if not rule.get("examples", {}).get("compliant"):
        problems.append("missing compliant example")

    return problems
