"""
agent.rule_enhance
==================
Auto-enhance rules: fill missing examples, improve statements,
and strengthen rule quality using LLM analysis.

Given a rule pack and (optionally) RTL source, this module:

  1. Identifies rules that are incomplete (missing examples, weak
     rationale, ungradeable predictions).
  2. Uses an LLM to generate violating/compliant code examples,
     improve statements, and suggest measurement predictions.
  3. Applies enhancements to the pack and persists them.

Enhancement is non-destructive: it only ADDS content to rules
(examples, rationale, predictions). It never removes or weakens
existing content.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from agent.llm import LLMCall
from agent import llm as _llm
from agent.llm.transports import run_briefs_concurrently


# ---------------------------------------------------------------------------
# Schema for LLM enhancement output
# ---------------------------------------------------------------------------

ENHANCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["violating_example", "compliant_example",
                 "improved_statement", "rationale_addition",
                 "suggested_predicts", "suggested_prevents"],
    "properties": {
        "violating_example": {
            "type": "string",
            "description": "SystemVerilog code snippet (5-15 lines) that violates this rule. Must be syntactically valid and clearly demonstrate the violation."
        },
        "compliant_example": {
            "type": "string",
            "description": "SystemVerilog code snippet (5-15 lines) that complies with this rule. Should be the corrected version of the violating example."
        },
        "improved_statement": {
            "type": "string",
            "description": "Improved rule statement. If the original is already good, return it unchanged."
        },
        "rationale_addition": {
            "type": "string",
            "description": "Additional rationale to append. Empty string if the existing rationale is sufficient."
        },
        "suggested_predicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "measurement_key": {"type": "string"},
                    "channel": {"type": "string"},
                    "value": {"type": "object"},
                    "horizon": {"type": "string"}
                }
            },
            "description": "Suggested predicts entries for ungradeable rules. Empty array if rule already has predictions."
        },
        "suggested_prevents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "downstream_stage": {"type": "string"},
                    "downstream_key": {"type": "string"},
                    "mechanism": {"type": "string"},
                    "estimated_cost_saved_hours": {"type": "number"}
                }
            },
            "description": "Suggested prevents entries explaining what downstream failures this rule prevents. Empty array if the rule already has prevents."
        },
        "quality_score": {
            "type": "integer",
            "description": "1-10 quality score for the ORIGINAL rule before enhancement."
        },
        "enhancement_notes": {
            "type": "string",
            "description": "Brief notes on what was enhanced and why."
        }
    }
}


# ---------------------------------------------------------------------------
# Diagnosis: which rules need enhancement?
# ---------------------------------------------------------------------------

@dataclass
class RuleGap:
    rule_id: str
    gaps: list[str]
    priority: int  # lower = more urgent


def find_gaps(pack: dict) -> list[RuleGap]:
    """Identify rules that are incomplete and need enhancement."""
    gaps_list: list[RuleGap] = []
    key_index = pack.get("key_index", {})

    for rule in pack.get("rules", []):
        if rule.get("status") == "retired":
            continue

        gaps: list[str] = []
        priority = 10

        examples = rule.get("examples", {})
        if not examples.get("violating"):
            gaps.append("missing_violating_example")
            priority = min(priority, 2)
        if not examples.get("compliant"):
            gaps.append("missing_compliant_example")
            priority = min(priority, 2)

        if not rule.get("predicts"):
            gaps.append("ungradeable_no_predicts")
            priority = min(priority, 1)

        statement = rule.get("statement", "")
        if len(statement) < 30:
            gaps.append("weak_statement")
            priority = min(priority, 3)

        if not rule.get("rationale"):
            gaps.append("missing_rationale")
            priority = min(priority, 4)

        if not rule.get("prevents"):
            gaps.append("missing_prevents")
            priority = min(priority, 5)

        for i, pred in enumerate(rule.get("predicts", [])):
            mk = pred.get("measurement_key", "")
            if mk and key_index and mk not in key_index:
                gaps.append(f"undeclared_key:{mk}")
                priority = min(priority, 3)

        if gaps:
            gaps_list.append(RuleGap(
                rule_id=rule["id"],
                gaps=gaps,
                priority=priority,
            ))

    gaps_list.sort(key=lambda g: g.priority)
    return gaps_list


# ---------------------------------------------------------------------------
# LLM enhancement call
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:60] or "x"


def _enhance_call(rule: dict, rtl_context: str = "") -> LLMCall:
    prompt = """# Role: RTL Rule Quality Enhancer

You are an expert RTL design verification engineer improving the quality
of rules in a verification knowledge base.

## Task

Given a rule, generate:
1. A **violating** SystemVerilog code example (5-15 lines) that clearly
   demonstrates the violation. Must be syntactically valid SV.
2. A **compliant** code example showing the correct way to write it.
3. An **improved statement** if the original is unclear or incomplete.
4. Additional **rationale** if the existing rationale is weak.
5. **Suggested predictions** if the rule has no `predicts[]` entries
   (measurement keys it should check against).
6. **Suggested prevents** if the rule has no `prevents[]` entries
   (what downstream failures this rule prevents --stage, key, mechanism).

## Guidelines

- Examples must be realistic RTL, not toy snippets. Include module
  declarations, proper signal types, and meaningful variable names.
- The violating example must clearly show WHY it violates the rule.
- The compliant example should be the minimal fix of the violating one.
- For statements, be precise about what constitutes a violation.
- For predictions, use standard measurement keys from the key_index:
  rtl.lint.*.count for lint checks, synth.* for synthesis, etc.
- For prediction channels, use ONLY these valid values:
  "intervals" (numeric range with min/max), "enum" (categorical values),
  "ranking" (ordered list), "includes" (must contain), "excludes" (must not contain).
  Most numeric rules use "intervals" with value {"min": N, "max": M}.
- For prevents, specify downstream_stage (synth/sim/sta/dft/silicon),
  downstream_key, and mechanism (how this rule prevents the failure).
- Quality score: 1=unusable, 5=functional but bare, 10=publication-ready.

## Inputs

Read `inputs.json`. It contains:
  - `rule`: the rule to enhance (full JSON)
  - `gaps`: list of identified gaps (e.g., "missing_violating_example")
  - `key_index`: available measurement keys in the pack
  - `rtl_context`: (optional) sample RTL from the design for context
"""
    inputs: dict[str, Any] = {
        "rule": rule,
        "gaps": [],
        "key_index": {},
    }
    if rtl_context:
        inputs["rtl_context"] = rtl_context
    return LLMCall(
        name=f"enhance.{_slug(rule.get('id', ''))}",
        model=_llm.MODEL_OPUS,
        role="rule_enhancer",
        prompt=prompt,
        schema=ENHANCE_SCHEMA,
        inputs=inputs,
    )


def _write_brief(call: LLMCall, run_dir: str) -> dict:
    paths = call.write_brief(run_dir)
    return {
        "name": call.name, "model": call.model, "role": call.role,
        "prompt": paths["prompt"], "schema": paths["schema"],
        "inputs": paths["inputs"], "output": paths["output"],
    }


def _read_output(call: LLMCall, run_dir: str) -> dict | None:
    out = os.path.join(run_dir, "llm_calls", call.name, "output.json")
    if not os.path.exists(out):
        return None
    with open(out) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Enhancement application
# ---------------------------------------------------------------------------

@dataclass
class Enhancement:
    rule_id: str
    added_violating: bool = False
    added_compliant: bool = False
    improved_statement: bool = False
    added_rationale: bool = False
    added_predicts: int = 0
    added_prevents: int = 0
    quality_before: int = 0
    notes: str = ""


def apply_enhancement(rule: dict, output: dict) -> Enhancement:
    """Apply LLM-generated enhancements to a rule dict (in place)."""
    enh = Enhancement(rule_id=rule["id"])
    enh.quality_before = output.get("quality_score", 0)
    enh.notes = output.get("enhancement_notes", "")

    examples = rule.setdefault("examples", {})

    violating = (output.get("violating_example") or "").strip()
    if violating and not examples.get("violating"):
        examples["violating"] = [violating]
        enh.added_violating = True

    compliant = (output.get("compliant_example") or "").strip()
    if compliant and not examples.get("compliant"):
        examples["compliant"] = [compliant]
        enh.added_compliant = True

    improved = (output.get("improved_statement") or "").strip()
    if improved and improved != rule.get("statement", ""):
        if len(improved) > len(rule.get("statement", "")) * 0.5:
            rule["statement"] = improved
            enh.improved_statement = True

    addition = (output.get("rationale_addition") or "").strip()
    if addition and addition not in (rule.get("rationale") or ""):
        existing = rule.get("rationale", "")
        if existing:
            rule["rationale"] = existing.rstrip() + " " + addition
        else:
            rule["rationale"] = addition
        enh.added_rationale = True

    _VALID_CHANNELS = {"intervals", "enum", "ranking", "includes", "excludes"}
    suggested = output.get("suggested_predicts", [])
    if suggested and not rule.get("predicts"):
        valid = []
        for sp in suggested:
            if (sp.get("measurement_key") and sp.get("channel")
                    and sp["channel"] in _VALID_CHANNELS):
                valid.append(sp)
        if valid:
            rule["predicts"] = valid
            enh.added_predicts = len(valid)

    prevents = output.get("suggested_prevents", [])
    if prevents and not rule.get("prevents"):
        valid_p = []
        for pp in prevents:
            if pp.get("downstream_stage") and pp.get("mechanism"):
                valid_p.append(pp)
        if valid_p:
            rule["prevents"] = valid_p
            enh.added_prevents = len(valid_p)

    if any([enh.added_violating, enh.added_compliant,
            enh.improved_statement, enh.added_rationale,
            enh.added_predicts, enh.added_prevents]):
        rule.setdefault("history", []).append({
            "event": "auto_enhanced",
            "at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(),
            "quality_before": enh.quality_before,
            "notes": enh.notes,
        })

    return enh


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

async def enhance_rules(pack: dict, run_dir: str, *,
                        rtl_context: str = "",
                        max_rules: int = 10,
                        concurrency: int = 4) -> list[Enhancement]:
    """Enhance incomplete rules using LLM calls.

    Finds gaps, sends enhancement requests to the LLM, and applies
    results back to the pack dict (in place).
    """
    os.makedirs(run_dir, exist_ok=True)

    all_gaps = find_gaps(pack)
    if not all_gaps:
        print("[enhance] all rules complete --nothing to enhance")
        return []

    to_enhance = all_gaps[:max_rules]
    rules_by_id = {r["id"]: r for r in pack.get("rules", [])}
    key_index = pack.get("key_index", {})

    calls = []
    for gap in to_enhance:
        rule = rules_by_id.get(gap.rule_id)
        if not rule:
            continue
        call = _enhance_call(rule, rtl_context)
        call.inputs["gaps"] = gap.gaps
        call.inputs["key_index"] = key_index
        calls.append((gap, call))

    if not calls:
        return []

    briefs = [_write_brief(c, run_dir) for _, c in calls]
    print(f"[enhance] running {len(briefs)} enhancement call(s)...",
          flush=True)
    await run_briefs_concurrently(briefs, concurrency=concurrency)

    enhancements: list[Enhancement] = []
    for gap, call in calls:
        out = _read_output(call, run_dir)
        if not out:
            continue
        rule = rules_by_id.get(gap.rule_id)
        if not rule:
            continue
        enh = apply_enhancement(rule, out)
        enhancements.append(enh)

    return enhancements


def enhance_rules_sync(*args, **kwargs) -> list[Enhancement]:
    return asyncio.run(enhance_rules(*args, **kwargs))


# ---------------------------------------------------------------------------
# Cross-validation: detect conflicts, duplicates, inverted logic
# ---------------------------------------------------------------------------

@dataclass
class RuleIssue:
    rule_id: str
    issue_type: str   # duplicate, conflict, inverted, orphan_key
    detail: str
    severity: str     # error, warning, info
    auto_fix: str     # retire, widen, none


def cross_validate(pack: dict) -> list[RuleIssue]:
    """Check rules against each other for conflicts and problems."""
    issues: list[RuleIssue] = []
    rules = [r for r in pack.get("rules", []) if r.get("status") != "retired"]
    key_index = pack.get("key_index", {})

    # Build maps
    by_key: dict[str, list[dict]] = {}
    for r in rules:
        for pred in r.get("predicts", []):
            mk = pred.get("measurement_key", "")
            if mk:
                by_key.setdefault(mk, []).append(r)

    # Check 1: duplicate predictions on same key with conflicting bands
    for mk, rule_list in by_key.items():
        if len(rule_list) < 2:
            continue
        bands = []
        for r in rule_list:
            for pred in r.get("predicts", []):
                if pred.get("measurement_key") == mk and pred.get("channel") == "intervals":
                    val = pred.get("value", {})
                    bands.append((r["id"], val.get("min"), val.get("max")))
        if len(bands) >= 2:
            for i in range(len(bands)):
                for j in range(i + 1, len(bands)):
                    id_a, min_a, max_a = bands[i]
                    id_b, min_b, max_b = bands[j]
                    if None in (min_a, max_a, min_b, max_b):
                        continue
                    if max_a < min_b or max_b < min_a:
                        issues.append(RuleIssue(
                            rule_id=id_a,
                            issue_type="conflict",
                            detail=f"band [{min_a},{max_a}] vs {id_b} [{min_b},{max_b}] on {mk}",
                            severity="warning",
                            auto_fix="none",
                        ))

    # Check 2: inverted logic --rules demanding min > 0 on lint/warning counts
    for r in rules:
        for pred in r.get("predicts", []):
            mk = pred.get("measurement_key", "")
            if pred.get("channel") != "intervals":
                continue
            val = pred.get("value", {})
            mn = val.get("min")
            if mn is not None and mn > 0:
                is_lint_like = any(t in mk for t in
                    ("lint", "warning", "error", "latch", "width"))
                if is_lint_like:
                    issues.append(RuleIssue(
                        rule_id=r["id"],
                        issue_type="inverted",
                        detail=f"demands min={mn} on '{mk}' --wrong rules want lint counts > 0",
                        severity="error",
                        auto_fix="retire",
                    ))

    # Check 3: orphan measurement keys not in key_index
    if key_index:
        for r in rules:
            for pred in r.get("predicts", []):
                mk = pred.get("measurement_key", "")
                if mk and mk not in key_index:
                    issues.append(RuleIssue(
                        rule_id=r["id"],
                        issue_type="orphan_key",
                        detail=f"key '{mk}' not in pack key_index",
                        severity="warning",
                        auto_fix="none",
                    ))

    # Check 4: duplicate statements (near-identical rules)
    seen_stmts: dict[str, str] = {}
    for r in rules:
        stmt = (r.get("statement") or "").lower().strip()
        if len(stmt) < 20:
            continue
        short = stmt[:80]
        if short in seen_stmts:
            issues.append(RuleIssue(
                rule_id=r["id"],
                issue_type="duplicate",
                detail=f"near-duplicate of {seen_stmts[short]}",
                severity="info",
                auto_fix="none",
            ))
        else:
            seen_stmts[short] = r["id"]

    return issues


def apply_auto_fixes(pack: dict, issues: list[RuleIssue]) -> list[str]:
    """Apply automatic fixes for issues that have auto_fix != 'none'."""
    rules_by_id = {r["id"]: r for r in pack.get("rules", [])}
    actions: list[str] = []
    now = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat()

    for issue in issues:
        if issue.auto_fix == "none":
            continue
        rule = rules_by_id.get(issue.rule_id)
        if not rule or rule.get("status") == "retired":
            continue

        if issue.auto_fix == "retire":
            rule["status"] = "retired"
            rule.setdefault("history", []).append({
                "event": "auto_retired",
                "at": now,
                "reason": issue.detail,
            })
            actions.append(f"RETIRED {issue.rule_id}: {issue.detail}")

    return actions


# ---------------------------------------------------------------------------
# Rule generation: propose new rules from pack analysis
# ---------------------------------------------------------------------------

GENERATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["new_rules"],
    "properties": {
        "new_rules": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "statement", "kind", "strength",
                             "predicts", "rationale", "examples"],
                "properties": {
                    "id": {"type": "string"},
                    "statement": {"type": "string"},
                    "kind": {"type": "string", "enum": ["constraint", "tendency", "heuristic"]},
                    "strength": {"type": "string", "enum": ["high", "medium", "low"]},
                    "rationale": {"type": "string"},
                    "predicts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "measurement_key": {"type": "string"},
                                "channel": {"type": "string"},
                                "value": {"type": "object"},
                                "horizon": {"type": "string"}
                            }
                        }
                    },
                    "prevents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "downstream_stage": {"type": "string"},
                                "downstream_key": {"type": "string"},
                                "mechanism": {"type": "string"}
                            }
                        }
                    },
                    "examples": {
                        "type": "object",
                        "properties": {
                            "violating": {"type": "array", "items": {"type": "string"}},
                            "compliant": {"type": "array", "items": {"type": "string"}}
                        }
                    },
                    "functional": {
                        "type": "object",
                        "description": "Optional functional check (pattern/sva/protocol mode)"
                    }
                }
            },
            "description": "New rules to add to the pack. Each must be complete and ready to use."
        },
        "analysis_notes": {
            "type": "string",
            "description": "Brief analysis of what coverage gaps were found."
        }
    }
}


def _generate_call(pack: dict, *, rtl_context: str = "",
                   existing_ids: set | None = None) -> LLMCall:
    prompt = """# Role: RTL Rule Generator

You are an expert RTL design verification engineer. Your job is to analyze
the current rule pack and identify COVERAGE GAPS --important RTL design
rules that are missing from the pack.

## Task

Analyze the existing rules and the available measurement keys. Propose
1-5 NEW rules that would strengthen the pack. Focus on:

1. **Functional intent rules** --rules that encode design correctness
   (FSM properties, protocol compliance, timing constraints), not just
   lint counter thresholds.
2. **Coverage gaps** --measurement keys in key_index that no rule predicts.
3. **Common RTL bugs** not covered --reset domain crossings, clock gating
   errors, FIFO overflow conditions, memory inference issues.

## Requirements for each new rule

- `id`: must start with `r_rtl_` and be unique (not in existing_ids)
- `statement`: precise, actionable --says exactly what constitutes a violation
- `kind`: constraint (must/must-not), tendency (should), or heuristic (guideline)
- `strength`: high (blocks tape-out), medium (blocks sign-off), low (advisory)
- `predicts[]`: at least one entry with measurement_key from key_index.
  Channel must be one of: "intervals" (numeric, value: {min, max}),
  "enum", "ranking", "includes", "excludes". Most rules use "intervals".
- `rationale`: WHY this matters for silicon quality
- `examples`: violating AND compliant SystemVerilog snippets
- `prevents[]`: what downstream cost this prevents (optional but preferred)
- `functional`: optional pattern/sva/protocol check section

## DO NOT generate rules that:
- Duplicate existing rules (check existing_ids)
- Are too generic ("write good RTL")
- Cannot be graded against any measurement key in key_index

## Inputs

Read `inputs.json`:
  - `existing_rules`: summary of all current rules (id + statement)
  - `existing_ids`: set of current rule IDs (do not duplicate)
  - `key_index`: available measurement keys
  - `uncovered_keys`: keys that no current rule predicts
  - `rtl_context`: (optional) sample RTL for design-specific rules
"""
    existing = existing_ids or set()
    rules_summary = [{"id": r["id"], "statement": r.get("statement", "")[:100]}
                     for r in pack.get("rules", [])
                     if r.get("status") != "retired"]

    covered_keys = set()
    for r in pack.get("rules", []):
        if r.get("status") == "retired":
            continue
        for p in r.get("predicts", []):
            mk = p.get("measurement_key")
            if mk:
                covered_keys.add(mk)
    key_index = pack.get("key_index", {})
    uncovered = [k for k in key_index if k not in covered_keys]

    inputs: dict[str, Any] = {
        "existing_rules": rules_summary,
        "existing_ids": sorted(existing),
        "key_index": key_index,
        "uncovered_keys": uncovered,
    }
    if rtl_context:
        inputs["rtl_context"] = rtl_context

    return LLMCall(
        name="generate_rules",
        model=_llm.MODEL_OPUS,
        role="rule_generator",
        prompt=prompt,
        schema=GENERATE_SCHEMA,
        inputs=inputs,
    )


@dataclass
class GenerationResult:
    added: list[str]
    skipped: list[tuple[str, str]]  # (id, reason)
    notes: str


def _valid_new_rule(rule: dict, existing_ids: set, key_index: dict) -> str | None:
    """Return None if valid, or a rejection reason."""
    rid = rule.get("id", "")
    if not rid:
        return "missing id"
    if rid in existing_ids:
        return f"duplicate id '{rid}'"
    if not rule.get("statement"):
        return "missing statement"
    if not rule.get("predicts"):
        return "no predicts (ungradeable)"
    if not rule.get("examples", {}).get("violating"):
        return "missing violating example"
    valid_ch = {"intervals", "enum", "ranking", "includes", "excludes"}
    for pred in rule.get("predicts", []):
        ch = pred.get("channel", "")
        if ch and ch not in valid_ch:
            pred["channel"] = "intervals"
    return None


def _make_full_rule(raw: dict) -> dict:
    """Build a complete v1 rule dict from LLM output."""
    now = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat()
    return {
        "id": raw["id"],
        "version": 1,
        "statement": raw.get("statement", ""),
        "kind": raw.get("kind", "constraint"),
        "strength": raw.get("strength", "medium"),
        "status": "active",
        "applies_to": {
            "stage": ["rtl"],
            "tools": ["verilator"],
            "pdks": [],
            "design_class": ["any"],
            "code_origin": ["any"],
        },
        "when": [{"op": "tag", "name": "rtl_stage"}],
        "unless": [],
        "predicts": raw.get("predicts", []),
        "prevents": raw.get("prevents", []),
        "rationale": raw.get("rationale", ""),
        "citations": [],
        "examples": raw.get("examples", {}),
        "functional": raw.get("functional"),
        "authored_by": "auto_generator",
        "authored_at": now,
        "history": [{"event": "auto_generated", "at": now}],
    }


async def generate_rules(pack: dict, run_dir: str, *,
                         rtl_context: str = "",
                         concurrency: int = 4) -> GenerationResult:
    """Generate new rules to fill coverage gaps."""
    os.makedirs(run_dir, exist_ok=True)
    existing_ids = {r["id"] for r in pack.get("rules", [])}
    key_index = pack.get("key_index", {})

    call = _generate_call(pack, rtl_context=rtl_context,
                          existing_ids=existing_ids)
    brief = _write_brief(call, run_dir)
    print("[generate] proposing new rules...", flush=True)
    await run_briefs_concurrently([brief], concurrency=1)

    out = _read_output(call, run_dir)
    if not out:
        return GenerationResult(added=[], skipped=[], notes="LLM call failed")

    added: list[str] = []
    skipped: list[tuple[str, str]] = []

    for raw in out.get("new_rules", []):
        reason = _valid_new_rule(raw, existing_ids, key_index)
        if reason:
            skipped.append((raw.get("id", "?"), reason))
            continue
        full = _make_full_rule(raw)
        if full.get("functional") is None:
            del full["functional"]
        pack.setdefault("rules", []).append(full)
        existing_ids.add(full["id"])
        added.append(full["id"])

    return GenerationResult(
        added=added,
        skipped=skipped,
        notes=out.get("analysis_notes", ""),
    )


def generate_rules_sync(*args, **kwargs) -> GenerationResult:
    return asyncio.run(generate_rules(*args, **kwargs))


# ---------------------------------------------------------------------------
# Autonomous loop: enhance + cross-validate + generate + self-correct
# ---------------------------------------------------------------------------

@dataclass
class AutoLoopReport:
    rounds: int = 0
    total_enhanced: int = 0
    total_generated: int = 0
    total_retired: int = 0
    total_issues_found: int = 0
    total_issues_fixed: int = 0
    initial_gaps: int = 0
    final_gaps: int = 0
    details: list[str] = None

    def __post_init__(self):
        if self.details is None:
            self.details = []


async def auto_loop(pack: dict, run_dir: str, *,
                    rtl_context: str = "",
                    max_rounds: int = 5,
                    batch_size: int = 10,
                    concurrency: int = 4,
                    do_generate: bool = True) -> AutoLoopReport:
    """Full autonomous rules loop.

    Repeats until all gaps are filled or max_rounds reached:
      1. Enhance existing rules (fill gaps in batches)
      2. Cross-validate (detect conflicts, inverted logic, duplicates)
      3. Auto-fix issues (retire wrong rules)
      4. Generate new rules (coverage gaps)
    """
    report = AutoLoopReport()
    report.initial_gaps = len(find_gaps(pack))

    for rnd in range(1, max_rounds + 1):
        report.rounds = rnd
        round_dir = os.path.join(run_dir, f"round_{rnd}")
        os.makedirs(round_dir, exist_ok=True)

        print(f"\n{'='*60}")
        print(f"  AUTO-ENHANCE ROUND {rnd}/{max_rounds}")
        print(f"{'='*60}")

        # --- Phase 1: Enhance existing rules ---
        gaps = find_gaps(pack)
        if gaps:
            print(f"\n[round {rnd}] {len(gaps)} rule(s) with gaps")
            enhancements = await enhance_rules(
                pack, round_dir,
                rtl_context=rtl_context,
                max_rules=batch_size,
                concurrency=concurrency,
            )
            applied = [e for e in enhancements
                       if any([e.added_violating, e.added_compliant,
                               e.improved_statement, e.added_rationale,
                               e.added_predicts])]
            report.total_enhanced += len(applied)
            report.details.append(
                f"round {rnd}: enhanced {len(applied)} rule(s)")
            print(format_enhancements(enhancements))
        else:
            print(f"\n[round {rnd}] all existing rules complete")

        # --- Phase 2: Cross-validate ---
        issues = cross_validate(pack)
        report.total_issues_found += len(issues)
        if issues:
            print(f"\n[round {rnd}] cross-validation found {len(issues)} issue(s):")
            for iss in issues:
                marker = {"error": "ERROR", "warning": "WARN ", "info": "INFO "}
                print(f"  [{marker.get(iss.severity, '?????')}] "
                      f"{iss.rule_id}: {iss.issue_type} --{iss.detail}")

            # Auto-fix what we can
            actions = apply_auto_fixes(pack, issues)
            report.total_issues_fixed += len(actions)
            report.total_retired += sum(1 for a in actions if a.startswith("RETIRED"))
            for a in actions:
                report.details.append(f"round {rnd}: {a}")
                print(f"  -> {a}")

        # --- Phase 3: Generate new rules (only on last round or if gaps are done) ---
        remaining_gaps = find_gaps(pack)
        if do_generate and (not remaining_gaps or rnd == max_rounds):
            gen_dir = os.path.join(round_dir, "generate")
            result = await generate_rules(
                pack, gen_dir,
                rtl_context=rtl_context,
                concurrency=concurrency,
            )
            report.total_generated += len(result.added)
            if result.added:
                report.details.append(
                    f"round {rnd}: generated {len(result.added)} new rule(s): "
                    + ", ".join(result.added))
                print(f"\n[round {rnd}] generated {len(result.added)} new rule(s):")
                for rid in result.added:
                    print(f"  + {rid}")
            if result.skipped:
                for rid, reason in result.skipped:
                    print(f"  - skipped {rid}: {reason}")
            if result.notes:
                print(f"  notes: {result.notes}")

        # Check if we're done
        remaining = find_gaps(pack)
        report.final_gaps = len(remaining)
        if not remaining and not issues:
            print(f"\n[round {rnd}] all rules complete, no issues --done")
            break
        if remaining:
            print(f"\n[round {rnd}] {len(remaining)} gap(s) remaining")
        else:
            print(f"\n[round {rnd}] gaps filled, checking for new issues...")

    return report


def auto_loop_sync(*args, **kwargs) -> AutoLoopReport:
    return asyncio.run(auto_loop(*args, **kwargs))


# ---------------------------------------------------------------------------
# Console formatting
# ---------------------------------------------------------------------------

def format_gaps(gaps: list[RuleGap]) -> str:
    if not gaps:
        return "[enhance] all rules complete"
    lines = [f"Rules needing enhancement: {len(gaps)}"]
    for g in gaps:
        lines.append(f"  {g.rule_id}")
        for gap in g.gaps:
            lines.append(f"    - {gap}")
    return "\n".join(lines)


def format_enhancements(enhancements: list[Enhancement]) -> str:
    if not enhancements:
        return "[enhance] no enhancements applied"
    lines = [f"[enhance] {len(enhancements)} rule(s) enhanced:"]
    for e in enhancements:
        parts = []
        if e.added_violating:
            parts.append("+violating_example")
        if e.added_compliant:
            parts.append("+compliant_example")
        if e.improved_statement:
            parts.append("+statement")
        if e.added_rationale:
            parts.append("+rationale")
        if e.added_predicts:
            parts.append(f"+{e.added_predicts} predicts")
        if e.added_prevents:
            parts.append(f"+{e.added_prevents} prevents")
        if parts:
            lines.append(f"  {e.rule_id}: {', '.join(parts)}"
                         f"  (was quality {e.quality_before}/10)")
    return "\n".join(lines)


def format_auto_report(report: AutoLoopReport) -> str:
    lines = [
        "",
        "=" * 60,
        "  AUTO-ENHANCE COMPLETE",
        "=" * 60,
        f"  Rounds run       : {report.rounds}",
        f"  Rules enhanced   : {report.total_enhanced}",
        f"  Rules generated  : {report.total_generated}",
        f"  Rules retired    : {report.total_retired}",
        f"  Issues found     : {report.total_issues_found}",
        f"  Issues fixed     : {report.total_issues_fixed}",
        f"  Gaps: {report.initial_gaps} -> {report.final_gaps}",
    ]
    if report.details:
        lines.append("")
        lines.append("  Actions taken:")
        for d in report.details:
            lines.append(f"    {d}")
    lines.append("=" * 60)
    return "\n".join(lines)
