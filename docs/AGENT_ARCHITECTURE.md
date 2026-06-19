# Cogni — Agent Architecture

This is the engineering reference for how Cogni's verdict layer is wired together.
It documents the modules in `agent/`, how a question travels through them, what gets
written to disk, and the contracts between components. Read this once before
touching the orchestrator or organs.

The verdict layer is deliberately small. It does **not** run any EDA tool, simulate
a circuit, or guess at numbers. It **predicts** measurable outcomes from a
knowledge pack, **commits** the prediction to an immutable ledger, lets a
domain-specific oracle observe reality, then **scores** prediction vs. reality and
**reflects** by proposing pack edits.

## 1. The Cognitive Loop

```
                       ┌────────────────────────────────────────┐
                       │            World Model (W)             │
                       │  facts: { measurement_key -> Fact }    │
                       │  tags : set[str]   stage: str          │
                       └──────────────────────┬─────────────────┘
                                              │
   Pack(rules, key_index)  ──►  Recall   ──►  candidates : list[Rule]
                                              │
                                          Attention            (LLM)
                                              │
                                          focused_rules + focused_facts
                                              │
                                          Predictor            (LLM)
                                              │
                                       Prediction OR Refusal
                                              │
                                     Verifier panel (×N)       (LLM, parallel)
                                              │
                              any dissent? ── yes ──► Revision   (LLM, once)
                                              │
                                          Commit to ledger.jsonl
                                              │
                                Adapter.observe(world)
                                              │
                                          Reality (ground truth)
                                              │
                                       Verdict scorer
                                              │
                                          Reflector            (LLM)
                                              │
                                       KB edits applied → pack saved
```

Each box that says "(LLM)" is one structured call materialized as an `LLMCall`
object with `model`, `system`, `user`, `response_schema`, then dispatched.

## 2. Module Map

| File | Role | Lines |
|------|------|------|
| `agent/core.py` | Dataclasses for Rule, World, Prediction, Refusal, Verdict, KBEdit, etc. Single source of truth for enums (`VerdictKind`, `RuleStrength`, `Confidence`, `RuleStatus`). | ~350 |
| `agent/kb.py` | `KnowledgeBase` — load v0/v1 JSON pack, auto-upgrade v0→v1, recall by stage+tags, apply KBEdits, save back to disk. Handles list-of-dict v1 `unless` semantics. | ~310 |
| `agent/kb_schema.json` | JSON Schema (draft-07) for v1 packs. Validated on load. | — |
| `agent/predicates.py` | Pure-Python evaluator for `when`/`unless` predicates. Closed op set: `eq, neq, lt, lte, gt, gte, in, not_in, has_tag, missing_tag, between`. | ~165 |
| `agent/perceiver.py` | Calls the active adapter's `perceive()` to turn raw scenario inputs into a `WorldModel`. Auto-injects `<stage>_stage` tag when missing. | ~45 |
| `agent/organs.py` | Builds the `LLMCall` for each cognitive stage: `attention_call`, `predictor_call`, `verifier_calls`, `revision_call`, `reflector_call`. All schemas live here. | ~490 |
| `agent/orchestrator.py` | The cognitive loop itself. `Orchestrator.cycle()` runs perceive→…→commit; `Orchestrator.reflect()` runs reflection and applies edits. | ~300 |
| `agent/dispatcher.py` | Two implementations of `Dispatcher`: a real LLM-API dispatcher and a record/replay dispatcher used by tests. Concurrency, retries, cost accounting live here. | ~305 |
| `agent/verdict.py` | `score(prediction, reality)` → `Verdict`. Owns the closed `VerdictKind` lattice (right_and_right_reason, right_but_wrong_reason, wrong_but_right_direction, wrong_and_wrong_reason, unfalsifiable). | ~450 |
| `agent/observability.py` | Cost & latency aggregation, per-stage rollups for `summary.json`. | ~230 |
| `adapters/<stage>/<tool>/perceiver.py` | Reads scenario raw inputs, returns `WorldModel`. `stage` and `tool` are class attributes. | — |
| `adapters/<stage>/<tool>/oracle.py` | Reads or replays tool outputs, returns `Reality` (a flat dict of `measurement_key -> value` and a `string_summary`). | — |

## 3. Data Contracts

### 3.1 `WorldModel`
```python
@dataclass
class WorldModel:
    facts: dict[str, Fact]   # measurement_key -> Fact(value, source, confidence)
    tags : set[str]
    stage: str | None
```
`measurement_key` is namespaced lowercase: `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`,
e.g. `rtl.lint.latch.count`, `synth.ff_share_pct`, `pdk.band`.

### 3.2 v1 `Rule` (canonical pack format — see `docs/KB_RULE_FORMAT.md`)
```json
{
  "id": "r_rtl_assignment_discipline_by_process_type",
  "kind": "constraint",                       // constraint | tendency | heuristic | identity
  "stage": "rtl",                             // closed enum, see KB_RULE_FORMAT.md
  "code_origin": "any",                       // any | human | ai_generated | ai_assisted | legacy_imported
  "strength": "high",                         // high | medium | low  (→ STRONG | TENDENCY | HEURISTIC internally)
  "statement": "Use '<=' inside always_ff and '=' inside always_comb.",
  "when":  [{"kind":"has_tag","value":"rtl_stage"}],
  "unless":[{"kind":"missing_tag","value":"intended_flop_based"}],
  "predicts": [
    { "key":"rtl.lint.blkseq.count", "op":"between", "lo":0, "hi":0,
      "violation_count_key":"rtl.lint.blkseq.count" }
  ],
  "prevents": ["sim_synth_mismatch_in_ff_path"],
  "citations": [ ... ]
}
```

`predicts[*]` is what the predictor is **allowed** to assert against. The
verdict scorer reads the same field — so a rule that doesn't predict anything
measurable cannot earn a `right_and_right_reason` verdict.

### 3.3 `Reality`
Flat `dict[str, JSON]` plus an optional `string_summary`. The oracle never
shapes it as rule-specific output — it just reports what the tool emitted, in the
same `measurement_key` namespace the pack uses. This decoupling is the whole
reason the verdict layer is portable across stages.

### 3.4 `Verdict`
```python
class VerdictKind(Enum):
    RIGHT_AND_RIGHT_REASON     = "right_and_right_reason"
    RIGHT_BUT_WRONG_REASON     = "right_but_wrong_reason"
    WRONG_BUT_RIGHT_DIRECTION  = "wrong_but_right_direction"
    WRONG_AND_WRONG_REASON     = "wrong_and_wrong_reason"
    UNFALSIFIABLE              = "unfalsifiable"
```
`unfalsifiable` is reserved for predictions that referenced no measurable key,
or whose key is absent from reality. It is not a failure mode — it is a
diagnostic that the pack's `predicts[*]` is too thin.

## 4. Pack Loading & Recall

`KnowledgeBase.load(path)`:
1. Read JSON, validate against `kb_schema.json` if `version == "1"`.
2. If `version` missing or `"0"`, run `_upgrade_v0_to_v1()` in-memory (idempotent).
3. Build `key_index: dict[measurement_key, list[rule_id]]` from every rule's
   `predicts[*].key` and `unless`/`when` keys. Recall is two operations:
   - filter by stage match (`stage == None` matches everything),
   - filter by `evaluate_predicate(when, world) == True` and
     `evaluate_predicate(unless, world) == False`.

There is no priority, no rule scoring, no learned weighting. Strength only feeds
the verifier's prompt as language ("strong/tendency/heuristic"), not the
recall set.

## 5. Cognitive Stages

### 5.1 Attention
- Input: candidate rules (post-recall), full world model, the question.
- Output: `focused_rule_ids`, `focused_fact_keys`, `rationale`.
- Purpose: when 10–30 rules survive recall, the predictor needs ≤ ~6.
- Empty focus → refusal (`refused_no_focus`).

### 5.2 Predictor
- Input: focused rules + focused facts + the question.
- Output: either `decision: refuse` with `missing_evidence`, or a `Prediction`
  with `claim`, `rationale`, `confidence`, `falsifier`, `cited_rule_ids`,
  `quantitative` (a `{key, op, value/lo/hi}` block aligned to one rule's
  `predicts[*]`).
- The prompt explicitly tells the predictor to count violations on diagnostic
  questions, not assert that `kind:constraint` rules are obeyed (that fix is
  F-006 in `FIXES_LOG.md`).

### 5.3 Verifier panel
- Two verifiers run in parallel against the prediction. They see the same
  rules and facts but a stripped prompt. Each returns `agrees: bool`,
  `concerns`, `suggested_revisions`.
- If any disagree → trigger one revision round.

### 5.4 Revision
- One shot. Verifier feedback + prior prediction in. Either a new prediction
  with `revisions=1` or a refusal.

### 5.5 Reality + Verdict
- `adapter.observe(world)` returns Reality. Verdict scorer reads both the
  prediction's `quantitative` block and `cited_rule_ids[*].predicts` to decide
  which `VerdictKind` applies.
- Right-and-right requires both the value to fall inside the predicted band
  **and** the cited rule's `predicts[*]` band/op to match.

### 5.6 Reflection
- Sees prediction, reality, verdict, cited rules.
- Output: `rule_attribution: {rule_id -> supported|failed|neutral}`,
  optional `surprise`, optional `kb_edits[*]` of kinds
  `add_rule | weaken_rule | strengthen_rule | append_unless | retire_rule`.
- Each edit is applied via `KnowledgeBase.apply()` and persisted with `kb.save()`.
- Outcomes are recorded in `RulePerformance` counters on each rule and saved
  back to the pack — this is the persistent learning channel.

## 6. Persistence Layout

For a single session at `runs/real/<timestamp>/`:

```
<scenario>_world.json          # snapshot of WorldModel at perceive time
<scenario>_reality.json        # snapshot of Reality at observe time
<scenario>_kb.json             # pack as loaded
<scenario>_kb_after.json       # pack after applied edits
ledger.jsonl                   # immutable: every Prediction (one line each)
refusals.jsonl                 # immutable: every Refusal
verdicts.jsonl                 # one line per scored prediction
surprises.jsonl                # one line per wrong/right-but-wrong-reason
kb_edits.jsonl                 # one line per applied KBEdit
llm_calls/<call_id>.json       # one file per LLM call: prompt, schema, raw, parsed
summary.json                   # final per-question record + cost rollups
pending_attention.json         # mid-pipeline state (resume point)
pending_predict.json           # mid-pipeline state (resume point)
pending_verify.json            # mid-pipeline state (resume point)
```

Resumption is file-based: each `run_real.py` subcommand is idempotent and
keys off the latest pending file, so a crash mid-cycle just costs the
in-flight LLM call.

## 7. Dispatcher

`agent/dispatcher.py` exposes:
- `LLMDispatcher` — production. Concurrency, retries on 429/5xx, cost meter.
- `RecordReplayDispatcher` — used by tests. Reads canned outputs from disk,
  no API calls.

The orchestrator only knows the `Dispatcher` Protocol; swapping in a local
SDK-backed dispatcher would be a one-file change.

## 8. What Lives Outside the Verdict Layer

- **No tool execution.** Verilator/Yosys/PnR are run by the user (or a CI
  runner) and the output is dropped into the scenario as `findings.json` or
  raw logs. The oracle reads them.
- **No rule mining.** Reflection proposes edits from the LLM's reasoning;
  there is no automated lint-output → rule extractor.
- **No probability distributions.** Strength is a 3-level enum, confidence
  is a 4-level enum. We never emit `p=0.83`.
- **English-only for v1.** Pack statements, citations, rationales.

## 9. Test Surface

`tests/` covers:
- predicate evaluator (every op × kind matrix),
- v0→v1 upgrade idempotence,
- `predicts[*]` round-trip through predictor → verdict scorer,
- `code_origin` filter,
- `prevents` flow into reflection prompt,
- KB apply for every `KBEditKind`,
- list-of-dict `unless` v1 semantics (regression for F-008),
- adapter discriminator tags.

73 tests, all green as of phase 3c reference run.
