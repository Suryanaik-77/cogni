"""
cogni.agent.organs
==================
The four LLM-driven organs: attention, predictor, verifier panel, reflector.

Each function builds an `LLMCall` (or several) — a typed, schema-enforced
task spec the orchestrator dispatches to a subagent. The organs themselves
are pure: given inputs, return calls. They never spawn subagents directly.
"""
from __future__ import annotations
import os
from .core import (
    WorldModel, Rule, Prediction, Refusal, VerifierVerdict,
    Reality, Verdict, VerdictKind, Surprise, KBEdit, KBEditKind,
    Confidence, RuleStrength, new_id,
)
from .llm import LLMCall, MODEL_OPUS, MODEL_GPT, MODEL_GEMINI


# ============================================================================
# Attention controller
# ============================================================================
# Given a WorldModel + question + all candidate rules, produce a "focus mask":
# which subset of facts and rules deserve the predictor's attention. This is
# what a senior engineer does before reasoning — they don't load the entire
# KB, they decide what's relevant first.

ATTENTION_SCHEMA = {
    "type": "object",
    "required": ["focused_fact_keys", "focused_rule_ids", "ignored_rule_ids", "rationale"],
    "properties": {
        "focused_fact_keys": {"type": "array", "items": {"type": "string"}},
        "focused_rule_ids": {"type": "array", "items": {"type": "string"}},
        "ignored_rule_ids": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
}


def attention_call(world: WorldModel, candidate_rules: list[Rule],
                   question: str, stage: str | None) -> LLMCall:
    prompt = f"""# Role: Attention Controller

You are the attention organ of a cognitive agent. Before the predictor
answers a question, you decide what subset of the world model and
candidate rules deserves focus. A senior engineer does this implicitly;
you do it explicitly so the framework can audit it.

## Question
{question}

## Stage
{stage or "(none)"}

## Your task
1. Read the world model facts and the candidate rules in `inputs.json`.
2. Pick a focused subset:
   - `focused_fact_keys`: the fact keys that are directly relevant to this question.
   - `focused_rule_ids`: the rule ids whose claims could plausibly answer or constrain this question.
   - `ignored_rule_ids`: rules that match by tags but are off-topic for this question.
3. Briefly explain why (one paragraph, peer-engineer voice — no consultant register).

## Discipline
- Be aggressive about ignoring. Most rules are off-topic for any single question.
- Do not invent fact keys or rule ids that aren't in the inputs.
- The output must validate against the schema.
"""
    inputs = {
        "world_facts": {k: v.value for k, v in world.facts.items()},
        "world_tags": sorted(world.tags),
        "candidate_rules": [
            {"id": r.id, "statement": r.statement, "when": r.when, "stage": r.stage,
             "strength": r.strength.value} for r in candidate_rules
        ],
        "question": question,
        "stage": stage,
    }
    return LLMCall(name=f"attention.{_slug(question)}", model=MODEL_OPUS,
                   role="attention controller", prompt=prompt, schema=ATTENTION_SCHEMA,
                   inputs=inputs)


# ============================================================================
# Predictor
# ============================================================================
# Given a focused world subset + focused rules + a question, produce a
# prediction OR a refusal. Refusal is a first-class output, not an error.

PREDICTOR_SCHEMA = {
    "type": "object",
    "required": ["decision"],
    "properties": {
        "decision": {"type": "string", "enum": ["predict", "refuse"]},
        "claim": {"type": "string"},
        "rationale": {"type": "string"},
        "confidence": {"type": "string", "enum": ["certain", "confident", "likely", "uncertain"]},
        "falsifier": {"type": "string"},
        "cited_rule_ids": {"type": "array", "items": {"type": "string"}},
        "quantitative": {"type": "object"},
        # Structured claim — used by the mechanical verdict engine. All keys
        # optional; the engine tries each channel that is present. The natural
        # language `claim` is kept for prompts and the reflector.
        "structured_claim": {
            "type": "object",
            "properties": {
                # Numeric intervals: {"area_um2": [110000, 160000], ...}
                "intervals": {"type": "object"},
                # Enum/categorical: {"top_module": "ibex_multdiv_fast"}
                "enum": {"type": "object"},
                # Ranking: {"set_key": "module_ranking", "target": "ibex_lsu",
                #          "position_band": [4, 9]}  -- target ranks within band
                "ranking": {"type": "object"},
                # Substrings the agent expects reality summary to CONTAIN.
                "includes": {"type": "array", "items": {"type": "string"}},
                # Substrings the agent expects reality summary to NOT contain.
                "excludes": {"type": "array", "items": {"type": "string"}},
            },
        },
        "refusal_reason": {"type": "string"},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "rules_considered_but_rejected": {"type": "array", "items": {"type": "string"}},
    },
}


def predictor_call(world: WorldModel, focused_rules: list[Rule],
                   focused_facts: dict, question: str, stage: str | None,
                   measurement_key_hint: str | None = None) -> LLMCall:
    """Build a predictor LLMCall.

    `measurement_key_hint` (when provided) is the exact key reality will
    store the answer under. Pass it from the scenario's verdict spec so
    the predictor uses the same key name in `structured_claim.intervals`
    / `enum` / `ranking.set_key`. Without this hint, the predictor
    invents key names that the verdict engine can't grade against.
    """
    key_hint_block = (
        f"\n## Measurement key (use this exact name in structured_claim)\n"
        f"`{measurement_key_hint}` \u2014 reality will store the gradable\n"
        f"value here. When you fill `intervals`, `enum`, or\n"
        f"`ranking.set_key`, use this exact key. If you use a different\n"
        f"name, the grader cannot find the value and the verdict becomes\n"
        f"UNFALSIFIABLE.\n"
        if measurement_key_hint else ""
    )
    prompt = f"""# Role: Predictor (primary cognitive model)

You are the predictor organ. Given a focused subset of the world model
and applicable rules, you either commit a falsifiable prediction OR
explicitly refuse.

## Question
{question}

## Stage
{stage or "(none)"}
{key_hint_block}

## CRITICAL DISCIPLINE — read carefully

**You must REFUSE if any of these hold:**
- No focused rule directly addresses the question.
- The applicable rules contradict each other and you cannot resolve.
- The world model is missing a fact you would need to predict.
- Your honest confidence is below "uncertain" (i.e. <50%).

**Refusing is a first-class outcome.** It is not failure. It is the
agent knowing what it doesn't know. Prefer a clean refusal with a
list of missing evidence over a low-confidence guess.

**If `focused_facts` contains `rtl.source`, the actual RTL code is in
front of you — READ IT.** Do not refuse a count question ("how many
latches / width warnings / blocking-in-always_ff / cases without default")
with "I cannot see the code": you can. Read the source, find the concrete
instances, and commit a count. Reason like a reviewer: a `case` over an
N-bit enum that lists all 2^N values is complete even with no `default`;
a signal not assigned on every arm of an `always_comb` is a latch
candidate; a blocking `=` inside `always_ff` is a blkseq hazard; an
operator comparing a small vector against a 32-bit `int`/parameter is a
width-mismatch candidate. You may still be wrong — the tool computes its
own answer and may score 0 where the code merely LOOKS hazardous — but
that is a falsifiable prediction, which is exactly what is wanted. Only
refuse a source-backed count question if the source is truncated past the
relevant logic or genuinely ambiguous.

**If you predict:**
- The claim must be falsifiable — reality must be able to prove it wrong.
- State the falsifier explicitly: what observation would refute the claim.
- Cite the rule ids you leaned on. Do not invent rule ids.
- Pick a confidence level: certain (>90%) | confident (~80%) | likely (~65%) | uncertain (~50%).
- Reason in peer-engineer voice. No consultant register, no exclamation points.

**Structured claim (mandatory if you predict):**
The natural-language `claim` is for humans. Reality is graded mechanically
from `structured_claim`. Fill the channels that fit your prediction. You
may use multiple channels. The grader checks every channel you fill.

**Use the rules' `predicts` keys.** Each focused rule may carry a
`predicts` list of objects shaped `{{measurement_key, channel, value,
horizon}}`. When you cite such a rule, key your `structured_claim` to
those exact `measurement_key`s so the grader can match. Do not invent
new key names. If the rule's `predicts` is empty, fall back to the
`measurement_key` shown in the "Measurement key" block above (or refuse
if neither is present).

**Tool-behavior rules win "what will the tool report" questions.**
Some questions ask what a specific TOOL will measure — "how many latches
will *Verilator* infer", "how many WIDTH warnings will *Verilator* emit".
These are questions about the TOOL'S OUTPUT, not about design correctness.
If a focused rule directly describes that tool's behavior on this exact
measurement key (e.g. a rule whose `predicts` is `rtl.lint.latch.count =
[0,0]` and whose statement says "Verilator does not flag this pattern"),
**that rule is the load-bearing answer — cite it and predict its value**,
even when a separate design rule says the code is hazardous. Both can be
true at once: the code is latch-prone AND this tool reports 0. The
question decides which you answer:
- "will the TOOL report / infer / emit / flag X" → answer with the
  tool-behavior rule's value (often 0).
- "is the DESIGN correct / safe / latch-free" → answer with the design
  rule. Do not let a design rule override a tool-behavior rule on a
  tool-output question; that is exactly the miss that keeps recurring.

**Respect rule `kind`:**
- `constraint` rules are pass/fail.
  - If the world model and the question imply the design FOLLOWS the
    rule, predict the compliant value (e.g. `{{min:0, max:0}}` for a
    violation-count metric).
  - If the world model or the question explicitly DESCRIBES violations
    ("missing default", "missing else", "blocking in always_ff",
    "width mismatch on a + b"), the rule still applies — you predict
    a NON-ZERO violation count. Count one violation per concrete
    instance the question/world names. Use a tight interval like
    `[N, N]` when the count is clear; widen only if ambiguous.
  - Diagnostic/forensic questions about known-buggy code are valid
    predictor territory under constraint rules. Refusing them just
    because "the rule says zero when compliant" is wrong — count the
    violations and predict.
- `tendency` rules give a typical range; pick interval bounds that
  reflect the rule's published band.
- `heuristic` rules are categorical; use `enum` or `ranking`.
- `identity` rules don't predict on their own — cite them as supporting
  context, not as the load-bearing reason for a numeric claim.

- `intervals`: numeric bounds keyed by measurement name, e.g.
  `{{"area_um2": [110000, 160000], "precip_mm_24h": [0.0, 0.2]}}`.
  Use this whenever you can name a number.
- `enum`: exact categorical value(s), e.g. `{{"top_module": "ibex_multdiv_fast"}}`.
- `ranking`: when you claim a target lands in a position band of a set,
  e.g. `{{"set_key": "module_ranking", "target": "ibex_lsu", "position_band": [4, 9]}}`.
- `includes`: substrings reality's summary should CONTAIN if you are right.
  Use lowercase, prefer roots over inflections (e.g. `"dissipat"` not
  `"dissipated"`). For "no precip", include `"dry"`, `"clear"`, `"no precip"`.
- `excludes`: substrings reality's summary should NOT contain if you are
  right. Use this to encode negation cleanly. For "no precip", exclude
  `"rain"`, `"shower"`, `"thunder"`.

Make the structured_claim sharp enough that a python script can grade it
without reading the prose.

## Output

If you predict, set `decision: "predict"` and fill: claim, rationale,
confidence, falsifier, cited_rule_ids, quantitative (if numeric).

If you refuse, set `decision: "refuse"` and fill: refusal_reason,
missing_evidence (what would unblock), rules_considered_but_rejected.
"""
    inputs = {
        "question": question,
        "stage": stage,
        "focused_facts": focused_facts,
        "focused_rules": [_rule_view_for_predictor(r) for r in focused_rules],
    }
    return LLMCall(name=f"predict.{_slug(question)}", model=MODEL_OPUS,
                   role="predictor", prompt=prompt, schema=PREDICTOR_SCHEMA,
                   inputs=inputs)


def _rule_view_for_predictor(r: Rule) -> dict:
    """How a rule is shown to the predictor / verifier / revisor.

    For v1 rules we also surface `kind`, `predicts`, `prevents`, and a
    short examples block. The predictor uses `predicts[*].measurement_key`
    to key its `structured_claim.intervals` / `enum` / `ranking.set_key`,
    and `prevents` so it knows which downstream cost the rule guards.
    """
    base = {
        "id": r.id,
        "statement": r.statement,
        "strength": r.strength.value,
        "rationale": r.rationale,
        "citations": r.citations,
    }
    if r.schema_version >= 1:
        base["kind"] = r.kind
        base["applies_to"] = r.applies_to_v1
        if r.predicts:
            base["predicts"] = r.predicts
        if r.prevents:
            base["prevents"] = r.prevents
        # Trim examples to keep token cost down — show at most one per side.
        ex = r.examples or {}
        if ex.get("violating") or ex.get("compliant"):
            base["examples"] = {
                "violating": (ex.get("violating") or [])[:1],
                "compliant": (ex.get("compliant") or [])[:1],
            }
    return base


# ============================================================================
# Verifier panel
# ============================================================================
# Two independent models critique. Same prompt. Different architectures.
# Disagreement is signal.

VERIFIER_SCHEMA = {
    "type": "object",
    "required": ["agrees", "concerns", "suggested_revisions"],
    "properties": {
        "agrees": {"type": "boolean"},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "suggested_revisions": {"type": "array", "items": {"type": "string"}},
        "alternative_claim": {"type": "string"},
    },
}


def verifier_calls(prediction_payload: dict, focused_rules: list[Rule],
                   focused_facts: dict, question: str) -> list[LLMCall]:
    prompt = f"""# Role: Verifier (independent critic)

A primary predictor has produced a prediction. Your job is to critique it
honestly. You are not here to agree.

## Question the predictor was asked
{question}

## What the predictor said
See `inputs.json` -> `prediction`.

## Inputs you have access to
The same focused rules and facts the predictor used.

## Your task
1. Is the claim well-supported by the cited rules and the focused facts?
2. Did the predictor cite rules that don't actually apply?
3. Is the confidence level honest? (Is it overconfident? underconfident?)
4. Is the falsifier sharp enough that reality could refute the claim?
5. Did the predictor miss a rule or fact that would change the answer?

## Output discipline
- `agrees`: true only if you would commit the same prediction yourself.
  Disagreement is not rude — it's the point of the panel.
- `concerns`: specific, actionable. Avoid generic statements like "could be wrong."
- `suggested_revisions`: what should change. If you would refuse instead, say so.
- `alternative_claim`: optional — a stronger/sharper claim if you'd replace it.

Be a peer engineer, not a critic-for-critique's sake. If the prediction
looks right, say so cleanly.
"""
    inputs = {
        "question": question,
        "prediction": prediction_payload,
        "focused_facts": focused_facts,
        "focused_rules": [_rule_view_for_predictor(r) for r in focused_rules],
    }
    base_name = _slug(question)
    return [
        LLMCall(name=f"verify_gpt.{base_name}", model=MODEL_GPT,
                role="verifier (gpt)", prompt=prompt, schema=VERIFIER_SCHEMA, inputs=inputs),
        LLMCall(name=f"verify_gemini.{base_name}", model=MODEL_GEMINI,
                role="verifier (gemini)", prompt=prompt, schema=VERIFIER_SCHEMA, inputs=inputs),
    ]


# ============================================================================
# Predictor revision (after verifier dissent)
# ============================================================================

def revision_call(world: WorldModel, focused_rules: list[Rule], focused_facts: dict,
                  question: str, stage: str | None,
                  prior_prediction: dict, verifier_feedback: list[dict]) -> LLMCall:
    prompt = f"""# Role: Predictor (revision round)

Verifiers raised concerns about your prior prediction. Revise it once.

## Question
{question}

## Your prior prediction
See `inputs.json` -> `prior_prediction`.

## Verifier concerns
See `inputs.json` -> `verifier_feedback`.

## Your task
- If the concerns are valid, revise: produce a new prediction (or refuse).
- If the concerns are wrong, hold your ground but explain why.
- Either way, this is your last chance to change the prediction. The
  framework will commit your output to the immutable ledger.

## Discipline (same as before)
- Refuse cleanly if confidence drops below "uncertain".
- Sharpen the falsifier and rule citations.
- Peer-engineer voice.
"""
    inputs = {
        "question": question,
        "stage": stage,
        "focused_facts": focused_facts,
        "focused_rules": [_rule_view_for_predictor(r) for r in focused_rules
        ],
        "prior_prediction": prior_prediction,
        "verifier_feedback": verifier_feedback,
    }
    return LLMCall(name=f"revise.{_slug(question)}", model=MODEL_OPUS,
                   role="predictor (revision)", prompt=prompt, schema=PREDICTOR_SCHEMA,
                   inputs=inputs)


# ============================================================================
# Reflector
# ============================================================================
# After reality lands, attribute the verdict to cited rules and propose
# typed KB edits.

REFLECTOR_SCHEMA = {
    "type": "object",
    "required": ["rule_attribution", "kb_edits"],
    "properties": {
        "rule_attribution": {
            "type": "object",
        },
        "surprise": {
            "type": "object",
            "properties": {
                "what_we_expected": {"type": "string"},
                "what_actually_happened": {"type": "string"},
                "why_we_missed_it": {"type": "string"},
            },
        },
        "kb_edits": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "rationale"],
                "properties": {
                    "kind": {"type": "string", "enum": ["add", "strengthen", "weaken", "scope", "retire", "rewrite"]},
                    "target_rule_id": {"type": "string"},
                    "new_rule": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "statement": {"type": "string"},
                            "when": {"type": "array"},
                            "unless": {"type": "array"},
                            "stage": {"type": ["string", "null"]},
                            "strength": {"type": "string"},
                            "rationale": {"type": "string"},
                            "citations": {"type": "array"},
                            # Gradeable prediction(s) — what reality this rule
                            # commits to. Without this the rule can never be
                            # checked against a measurement.
                            "predicts": {
                                "type": "array",
                                # Every predicts item MUST carry the fields that
                                # make it gradeable. Without this, a model can
                                # emit `predicts:[{}]` — a non-empty list that
                                # passes a bare array check but has no
                                # measurement_key, so the rule is born
                                # ungradeable and is silently dropped by promote.
                                "items": {
                                    "type": "object",
                                    "required": ["measurement_key", "channel", "value"],
                                    "properties": {
                                        "measurement_key": {"type": "string"},
                                        "channel": {"type": "string",
                                                     "enum": ["intervals", "enum", "includes", "excludes"]},
                                        "value": {},
                                        "horizon": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "new_strength": {"type": "string", "enum": ["law", "strong", "tendency", "situational", "heuristic"]},
                    # Items SHOULD be plain string tags, but models sometimes
                    # emit objects here. Don't hard-fail the whole reflection on
                    # that — accept any item and coerce to a string at finalize.
                    "added_unless": {"type": "array"},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}


def reflector_call(prediction: dict, reality: dict, verdict: dict,
                   cited_rules: list[Rule]) -> LLMCall:
    prompt = """# Role: Reflector (the cognitive update organ)

A prediction has landed against reality. You are the organ that
updates the knowledge base from this experience.

## Your task

1. **Attribute the verdict to the cited rules.** For each cited rule,
   say whether it was supported, failed, or neutral on this case.
   Output format: `{"rule_attribution": {"rule_id_1": "supported", "rule_id_2": "failed", ...}}`

2. **If the prediction was wrong, name the surprise.** What did we expect,
   what actually happened, why did we miss it?

3. **Propose typed KB edits.** Choose from:
   - `add`: a brand-new rule (with full Rule schema in `new_rule`)
   - `strengthen` / `weaken`: change strength of an existing rule
   - `scope`: narrow an existing rule's applicability with new `unless` tags
   - `retire`: remove a rule that is no longer trustworthy
   - `rewrite`: replace an existing rule's statement (with `new_rule`)

   For each edit, give a one-sentence rationale.

## Every `add`/`rewrite` rule MUST be gradeable — fill `predicts`
A rule with no `predicts` can never be checked against reality and is dead
weight. So every `new_rule` you emit MUST carry a non-empty `predicts` list:

  `predicts: [{"measurement_key": "...", "channel": "intervals|enum|includes|excludes", "value": ..., "horizon": "rtl"}]`

- Key it to a measurement that the oracle ACTUALLY emits. Look at the
  `reality.measurements` keys in your input — use one of THOSE exact keys.
  Do not invent a key the tool never produces (that just yields perpetual
  `unfalsifiable` verdicts).
- `intervals` value = `{"min": N, "max": M}`; `enum` value = list of allowed.
- **Tool-coverage notes are the tricky case.** If the lesson is "tool X does
  not flag pattern Y" and reality measured 0, then make the rule predict that
  measured-0 outcome on the key the tool DOES emit — e.g.
  `{"measurement_key": "rtl.lint.latch.count", "channel": "intervals",
  "value": {"min": 0, "max": 0}, "horizon": "rtl"}` — so the note is a real,
  checkable claim about the tool's behavior ("this tool reports 0 here"),
  not unfalsifiable prose. The functional design rule stays separate and
  strong (see the oracle-is-a-tool discipline above).
- If you genuinely cannot key the rule to any emitted measurement, do NOT
  `add` it — say so in `surprise.why_we_missed_it` instead. An ungradeable
  rule is worse than no rule.

## Discipline
- Don't propose edits if the prediction was right and the reasoning was right.
  Right-and-right-reason verdicts strengthen rules through performance, not edits.
- Don't propose `add` for a rule that's already in the KB — strengthen instead.
- Don't propose `retire` after a single failure unless the rule is clearly
  contradicted by mechanism, not just outcome.
- Be specific. Vague rules don't help future predictions.

## The oracle is a TOOL, not design truth (read carefully — most important)
Reality here is a lint tool's output (e.g. Verilator). A lint tool is a
STRUCTURAL oracle: it reports the subset of hazards IT chooses to flag, with
its own coverage gaps and leniency. **It is not proof that the design is
functionally correct.** Before you weaken/scope/retire a rule, decide which
of these two situations you are in:

1. **The mechanism was wrong** — the design is genuinely fine; the predictor
   mis-read the code or the rule's premise doesn't hold here. → It is OK to
   `weaken`/`scope` the rule, citing the design fact that makes it safe.

2. **The tool under-reported** — the design DOES have the hazard the rule
   warns about, but this tool didn't flag it (measured count is LOWER than a
   correct design reading would predict; often 0). → **DO NOT weaken, scope,
   or retire the functional rule.** The rule was functionally right; the tool
   is lenient. Degrading a correct design rule because a tool has a blind spot
   actively corrupts the rulebook.

**Test to tell them apart:** re-read the cited design facts / source. If a
careful engineer would still call the code hazardous (e.g. a signal left
unassigned on one arm of an `always_comb` with no default is latch-prone by
language semantics, even if THIS tool emits 0 latches), you are in case 2.

**Rule of thumb:** when `measured < predicted` on a hazard/violation COUNT and
the design genuinely exhibits the pattern, prefer recording a TOOL-COVERAGE
observation — an `add` of a clearly tool-scoped note like "tool X does not
flag pattern Y" (with `when` gated on that tool) — over editing the functional
rule. Keep the design rule intact; capture the tool's behavior separately.
Never let a tool's leniency lower the strength of a functional design rule.

## Verdict trust (read carefully)
The verdict input includes `verdict_confidence` and `channel`:
- `high`: numeric/enum/ranking decided. Trust the verdict.
- `medium`: only includes/excludes substring channels decided. You may
  overrule with rationale if the prediction's mechanism clearly held in
  reality but the substring set didn't catch it.
- `low`: legacy fallback or text-only — advisory only. If reality and
  mechanism actually agree, attribute rules as `supported` and document
  the grader gap in `surprise.why_we_missed_it`. Do not punish a rule
  whose mechanism was confirmed just because the grader missed it.
"""
    inputs = {
        "prediction": prediction,
        "reality": reality,
        "verdict": verdict,
        "cited_rules": [r.to_dict() for r in cited_rules],
    }
    return LLMCall(name=f"reflect.{prediction.get('id', 'unk')[:24]}", model=MODEL_OPUS,
                   role="reflector", prompt=prompt, schema=REFLECTOR_SCHEMA, inputs=inputs)


# ============================================================================
# Helpers
# ============================================================================

def _slug(text: str) -> str:
    """Make a filesystem-safe short id from a question."""
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower())[:48].strip("_")
    return s or "unnamed"
