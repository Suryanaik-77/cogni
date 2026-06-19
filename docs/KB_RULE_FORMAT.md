# KB rule format — RTL + synth (FINAL)

Status: **finalized**. Implement against this; no more format debate.
Author: cogni team.
Scope: how a single rule is written on disk so one agent, one verdict
layer, and one observability stack cover every EDA stage from
**pre-synth RTL** through **synth** without stage-specific forks.
Designed in 2026 with three forces in mind:

1. **Left-shift is the dominant trend.** Modern flows predict
   downstream PPA/timing/congestion from RTL (RTL Architect,
   MasterRTL, RTLDistil, GNN-on-AIG). The KB has to express *predicts
   downstream key X from upstream facts Y*, not just *describes a
   tendency*.
2. **AI is writing more RTL each quarter.** AI-generated Verilog has
   characteristic error modes (latch inference, blocking-in-seq,
   reset-strategy slop, FSM encoding gaps). The KB has to mark those
   as a category, not bury them in prose.
3. **Sign-off catch-rate is what matters.** Whether a rule fires at
   RTL or synth is an implementation detail; what matters is whether
   the rule prevents an issue from reaching PnR. The format makes
   `prevents` first-class.

---

## 1. The rule object (v1)

A rule is one JSON object. Eight sections, all mandatory except
`telemetry` (which is auto-managed):

```jsonc
{
  // ---- 1. identity ----
  "id":         "r_rtl_no_inferred_latch",
  "version":    1,
  "statement":  "Well-formed RTL must not infer latches in always_comb or always_ff blocks.",
  "kind":       "constraint",        // see §2.1
  "strength":   "high",              // see §2.2
  "status":     "active",            // active | shadow | retired

  // ---- 2. applicability ----
  // Where in the flow does this rule fire? Closed enums everywhere.
  "applies_to": {
    "stage":         ["rtl"],                       // see §2.3
    "tools":         ["verilator", "slang"],        // [] = any tool
    "pdks":          [],                            // [] = any
    "design_class":  ["any"],                       // see §2.4
    "code_origin":   ["any"]                        // see §2.5  — NEW
  },

  // ---- 3. predicate (when does it fire?) ----
  // Tree of {op, ...} nodes evaluated against
  // (WorldModel.facts ∪ Reality.measurements ∪ scenario.target).
  "when":   [ { "op": "tag", "name": "rtl_stage" } ],
  "unless": [],

  // ---- 4. claim (what does it predict?) ----
  // The contract with the verdict layer. Each entry is one falsifiable
  // claim against a measurement_key in the standardized namespace.
  "predicts": [
    {
      "measurement_key": "rtl.lint.LATCH.count",
      "channel":         "intervals",
      "value":           { "min": 0, "max": 0 },
      "horizon":         "rtl"                       // see §2.6 — NEW
    }
  ],

  // ---- 5. shift-left linkage ----   NEW
  // What downstream issue does this rule prevent? This is the part
  // that makes the KB a left-shift instrument, not just a linter.
  "prevents": [
    {
      "downstream_stage": "synth",
      "downstream_key":   "synth.warnings.LATCH",
      "mechanism":        "Inferred latches break STA and CDC; catching them at RTL avoids a synth-stage rerun.",
      "estimated_cost_saved_hours": 4
    }
  ],

  // ---- 6. provenance ----
  "rationale":   "Inferred latches signal incomplete case/if coverage and break CDC + STA.",
  "citations":   [
    { "title": "Verilator warnings reference",
      "url":   "https://verilator.org/guide/latest/warnings.html" }
  ],
  "authored_by": "seed-pack",
  "authored_at": "2026-04-15T00:00:00Z",

  // ---- 7. examples (optional but recommended) ----  NEW
  // Tiny code-or-data snippets that make the rule self-documenting
  // for both humans AND the predictor LLM.
  "examples": {
    "violating": [
      "always_comb begin\n  if (sel) y = a;\n  // missing else -> latch on y\nend"
    ],
    "compliant": [
      "always_comb begin\n  y = '0;\n  if (sel) y = a;\nend"
    ]
  },

  // ---- 8. telemetry (APPEND-ONLY, agent-managed) ----
  "history": [
    /* { at, event: cited|edit|retire, session_dir, qid,
         verdict, evidence_key, evidence_value, ... } */
  ]
}
```

That's the whole shape. No surprises later.

---

## 2. Closed vocabularies

These are the only legal values. Anything else is a load-time error.

### 2.1 `kind`

| value         | meaning                                                       | typical channel    |
|---------------|---------------------------------------------------------------|--------------------|
| `constraint`  | Must hold (lint, CDC, synthesizability). Boolean.             | `intervals [0,0]`  |
| `tendency`    | Empirical bound that usually holds; intervals are soft.       | `intervals`        |
| `heuristic`   | Categorical guidance — "X is the dominant submodule".         | `enum`, `ranking`  |
| `identity`    | Definitional fact, not falsifiable on its own.                | (rarely predicts)  |

Why this matters: the verdict layer should grade a `constraint` violation
as `wrong_and_wrong_reason` even if the predictor was "close"; it should
allow `wrong_but_right_direction` only for `tendency` rules. Hard-coded
in one place, no per-rule fudging.

### 2.2 `strength`

Closed three-way: `high | medium | low`. Maps to expected predictor
confidence floor (`CERTAIN`/`CONFIDENT`/`LIKELY`). Picked over the old
four-way (`strong | tendency | weak | speculative`) because the
attention organ + reflect organ both need integer-comparable strength;
three buckets is enough resolution for KB diff narratives.

### 2.3 `applies_to.stage` (closed enum)

`pre_rtl | rtl | synth | pnr | cts | sta | sim | power | signoff`

Maps 1:1 to the adapter registry's `stage` key. Multiple stages allowed
for cross-stage rules (e.g. a rule that fires on RTL facts AND grades
against synth measurements lists `["rtl", "synth"]`). Attention organ
filters by intersection with the scenario's stage.

### 2.4 `applies_to.design_class` (open list, conventional values)

Conventional values to encourage consistency, but **open list** so the
field doesn't need a schema bump every time a new IP shows up:

`any | small_rv32_core | large_ooo_core | dsp | accelerator | sram |
io_pad | analog_wrapper | clock_gen | bus_fabric | nic`

Use `["any"]` when the rule is design-agnostic (most lint rules).

### 2.5 `applies_to.code_origin`  *(new for 2026)*

`any | human | ai_generated | ai_assisted | legacy_imported`

This is the AI-RTL handle. Some rules — "always check FSM
default-state assignment", "always check non-blocking in always_ff" —
fire harder against AI-generated RTL because LLMs systematically
mis-handle them. Marking this in the rule means:

- attention organ can up-weight AI-RTL rules when scenario declares
  `core.code_origin: ai_generated`;
- KB-edit history can show "this rule strengthened only on AI-RTL,
  unchanged on human RTL" — that's a real research insight, lost if
  we don't model it.

`["any"]` for rules where origin is irrelevant (most synth/area rules).

### 2.6 `predicts[*].horizon`  *(new)*

`rtl | synth | pnr | sta | sim | power | signoff`

The stage at which the prediction becomes gradable by an oracle. A
left-shift rule may fire at RTL (`applies_to.stage: ["rtl"]`) but its
prediction can only be checked after synth (`predicts[0].horizon:
"synth"`). The verdict layer uses `horizon` to know which oracle owns
the measurement; without it, every claim looks ungradable until you
reach the bottom of the flow.

This is the single most important addition over the v0 format. It is
what makes the KB a left-shift instrument.

---

## 3. The predicate language

`when`, `unless` are arrays of predicate nodes (implicitly AND-ed).
Each node is `{op, ...}`. Supported ops, with grades-of-1 examples:

| op        | shape                                            | example                                                          |
|-----------|--------------------------------------------------|------------------------------------------------------------------|
| `tag`     | `{op:"tag", name}`                               | `{op:"tag", name:"has_multiplier"}`                              |
| `eq` / `ne` | `{op, key, value}`                             | `{op:"eq", key:"core.width", value:32}`                          |
| `lt` / `lte` / `gt` / `gte` | `{op, key, value}`             | `{op:"gt", key:"rtl.module.max_comb_depth", value:12}`           |
| `in`      | `{op:"in", key, values:[...]}`                   | `{op:"in", key:"pdk.node_nm", values:[7,16,22]}`                 |
| `matches` | `{op:"matches", key, pattern}` (regex)           | `{op:"matches", key:"rtl.top", pattern:"^ibex_.*"}`              |
| `exists`  | `{op:"exists", key}`                             | `{op:"exists", key:"rtl.fsm.decoder"}`                           |
| `all`     | `{op:"all", preds:[...]}`                        | nest                                                             |
| `any`     | `{op:"any", preds:[...]}`                        | nest                                                             |
| `not`     | `{op:"not", pred:{...}}`                         | nest                                                             |

**Lookup order for `key`**:
1. `WorldModel.facts[key]` (perceiver-derived)
2. `Reality.measurements[key]` (oracle-derived; only available after grade)
3. `scenario.target[key]` (constraints declared in the scenario yaml)

Missing keys evaluate to `false` for the node (predicate is
*falsified*, not *errored*). This is deliberate: a rule must positively
match all its requirements; absence is not consent.

**Tags vs keys**: tags are the namespaced free-form vocabulary the
perceiver emits alongside facts. They exist for predicates whose
"feature" is a binary property, not a measurable value. A perceiver
emits both:
```python
world.add("core.multiplier", "FastMul", tags=["has_multiplier","multicycle_mul"])
```
The rule writer picks whichever is more natural. Both are first-class.

---

## 4. The measurement-key namespace

Rules predict into measurement keys; oracles produce them. **Lowercase
snake_case, dotted, units in the leaf** when ambiguous.

| prefix       | owner                            | example keys                                                                              |
|--------------|----------------------------------|-------------------------------------------------------------------------------------------|
| `rtl.*`      | RTL perceiver + RTL oracle       | `rtl.module.<m>.max_comb_depth`, `rtl.lint.LATCH.count`, `rtl.fsm.<m>.encoding`, `rtl.cdc.unsynchronized_count`, `rtl.reset.strategy` |
| `synth.*`    | synth perceiver + synth oracle   | `synth.total_cell_area_um2`, `synth.total_cells`, `synth.top_module_by_cells`, `synth.warnings.LATCH`, `synth.gate_counts`, `synth.critical_path_levels` |
| `pnr.*`      | PnR adapter                      | `pnr.utilization_pct`, `pnr.wirelength_um`, `pnr.congestion_h_pct`, `pnr.congestion_v_pct` |
| `cts.*`      | CTS adapter                      | `cts.skew_ps`, `cts.insertion_delay_ps`                                                   |
| `sta.*`      | STA adapter                      | `sta.wns_ps`, `sta.tns_ps`, `sta.failing_endpoints`, `sta.hold_violations`                |
| `sim.*`      | functional sim                   | `sim.cycles_to_pass`, `sim.coverage.line_pct`, `sim.coverage.toggle_pct`, `sim.coverage.fsm_state_pct` |
| `power.*`    | power estimation                 | `power.total_mw`, `power.dynamic_mw`, `power.leakage_mw`                                  |
| `core.*`     | design-intrinsic (perceiver)     | `core.width`, `core.pipeline_stages`, `core.has_multiplier`, `core.code_origin`           |
| `target.*`   | scenario-declared targets        | `target.fmax_ghz`, `target.node_nm`, `target.power_budget_mw`                             |
| `pdk.*`      | PDK metadata                     | `pdk.node_nm`, `pdk.name`, `pdk.process_corner`                                           |

**Stability rule**: once a key is used by a published rule, the prefix
contract is frozen. Adding new keys is fine; renaming a key requires a
migration entry in the pack's changelog. The verdict layer never
guesses; if a rule predicts `synth.cell_count` but the oracle produces
`synth.total_cells`, that's a load-time validation error.

---

## 5. Verdict-channel reference (already implemented)

`predicts[*].channel` ∈ `{intervals, enum, ranking, includes, excludes,
legacy}`. Re-stated here so rule authors don't have to chase the
verdict module:

| channel    | `value` shape                                   | passes when                                       |
|------------|-------------------------------------------------|---------------------------------------------------|
| `intervals`| `{min, max, unit?}`                             | reality value ∈ [min, max]                        |
| `enum`     | `[choice1, choice2, ...]` (predicted alternatives) | reality value ∈ list (case-insensitive substring OK) |
| `ranking`  | ordered list of strings                         | reality top-N ranking matches by prefix or set    |
| `includes` | `[term1, term2, ...]`                           | reality string contains all terms                 |
| `excludes` | `[term1, ...]`                                  | reality string contains none                      |
| `legacy`   | `{positive_substrings: [...]}`                  | back-compat with v0 `verdict.contains` questions  |

Rules with no `predicts[]` entries are loadable but ungradable —
they'll be tagged `unfalsifiable` whenever the predictor cites them.

---

## 6. Two filled-in worked examples

### 6.1 RTL — AI-generated-RTL latch hazard (constraint, with prevents)

```jsonc
{
  "id":        "r_rtl_no_inferred_latch_ai",
  "version":   1,
  "statement": "AI-generated combinational logic must not infer latches: every if/case path in always_comb must assign every output, or default-assign at the top of the block.",
  "kind":      "constraint",
  "strength":  "high",
  "status":    "active",

  "applies_to": {
    "stage":        ["rtl"],
    "tools":        ["verilator", "slang"],
    "pdks":         [],
    "design_class": ["any"],
    "code_origin":  ["ai_generated", "ai_assisted"]
  },

  "when": [
    { "op": "tag",    "name": "rtl_stage" },
    { "op": "exists", "key":  "rtl.always_comb_blocks" }
  ],
  "unless": [],

  "predicts": [
    { "measurement_key": "rtl.lint.LATCH.count",
      "channel": "intervals",
      "value":   { "min": 0, "max": 0 },
      "horizon": "rtl" }
  ],

  "prevents": [
    { "downstream_stage": "synth",
      "downstream_key":   "synth.warnings.LATCH",
      "mechanism":        "Latches detected at RTL avoid a synth re-run and prevent silent CDC/STA hazards downstream.",
      "estimated_cost_saved_hours": 4 },
    { "downstream_stage": "sta",
      "downstream_key":   "sta.failing_endpoints",
      "mechanism":        "Latch-based paths cause unpredictable timing arcs; eliminating at RTL removes a class of STA noise.",
      "estimated_cost_saved_hours": 8 }
  ],

  "rationale":   "LLM-generated combinational blocks frequently miss the else/default arm; latch inference is the dominant lint failure category in the published 2025 AI-RTL corpus studies.",
  "citations": [
    { "title": "Automatically Fix RTL Lint Violations with GenAI (DVCon Taiwan 2025)",
      "url":   "https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf" },
    { "title": "Verilator warnings reference (LATCH)",
      "url":   "https://verilator.org/guide/latest/warnings.html" }
  ],
  "authored_by": "seed-pack",
  "authored_at": "2026-04-15T00:00:00Z",

  "examples": {
    "violating": [
      "always_comb begin\n  if (sel)\n    y = a;     // no else, no default -> latch on y\nend"
    ],
    "compliant": [
      "always_comb begin\n  y = '0;       // default first\n  if (sel) y = a;\nend"
    ]
  },

  "history": []
}
```

### 6.2 RTL → synth left-shift — deep combinational depth predicts area + WNS

```jsonc
{
  "id":        "r_rtl_deep_comb_predicts_area_and_wns",
  "version":   1,
  "statement": "Modules with max combinational depth > 12 levels at RTL synthesize to disproportionately larger area on the dominant module and are likely to leave WNS negative at 1 GHz on 12nm-class nodes.",
  "kind":      "tendency",
  "strength":  "medium",
  "status":    "active",

  "applies_to": {
    "stage":        ["rtl", "synth"],
    "tools":        [],
    "pdks":         ["12nm","16nm","7nm","sky130","asap7"],
    "design_class": ["any"],
    "code_origin":  ["any"]
  },

  "when": [
    { "op": "gt", "key": "rtl.module.max_comb_depth", "value": 12 }
  ],
  "unless": [
    { "op": "lt", "key": "target.fmax_ghz", "value": 0.5 }
  ],

  "predicts": [
    { "measurement_key": "synth.top_module_by_cells",
      "channel": "enum",
      "value": ["the same module flagged by rtl.module.max_comb_depth"],
      "horizon": "synth" },
    { "measurement_key": "sta.wns_ps",
      "channel": "intervals",
      "value":   { "min": -9999, "max": 0 },
      "horizon": "sta" }
  ],

  "prevents": [
    { "downstream_stage": "sta",
      "downstream_key":   "sta.failing_endpoints",
      "mechanism":        "Detecting deep comb chains at RTL flags re-pipelining work before synth, avoiding a costly STA-driven re-RTL cycle.",
      "estimated_cost_saved_hours": 16 }
  ],

  "rationale":   "Comb depth at RTL is a known leading indicator of post-synth critical-path levels; modern shift-left predictors (RTL-Timer, RTLDistil, MasterRTL) exploit exactly this signal.",
  "citations": [
    { "title": "RTLDistil (ICML 2025)",
      "url":   "https://icml.cc/virtual/2025/poster/43998" },
    { "title": "Shift-Left Techniques in EDA (arXiv 2509.14551)",
      "url":   "https://arxiv.org/abs/2509.14551" }
  ],
  "authored_by": "seed-pack",
  "authored_at": "2026-04-15T00:00:00Z",
  "examples":   { "violating": [], "compliant": [] },
  "history":    []
}
```

### 6.3 Synth — area scaling (the existing v0 rule, ported faithfully)

```jsonc
{
  "id":        "r_synth_area_ibex_m_ff_130nm",
  "version":   1,
  "statement": "On 130nm-class standard-cell flows, a small in-order RV32 core with M-extension and FF regfile synthesizes to ~110k–160k um² of cell area; the multiplier and 32x32 FF regfile dominate.",
  "kind":      "tendency",
  "strength":  "high",
  "status":    "active",

  "applies_to": {
    "stage":        ["synth"],
    "tools":        ["yosys"],
    "pdks":         ["sky130","asap7","nangate45"],
    "design_class": ["small_rv32_core"],
    "code_origin":  ["any"]
  },

  "when": [
    { "op": "tag", "name": "has_multiplier" },
    { "op": "tag", "name": "has_register_file" },
    { "op": "tag", "name": "small_core" }
  ],
  "unless": [
    { "op": "in", "key": "pdk.node_nm", "values": [7, 16, 22] }
  ],

  "predicts": [
    { "measurement_key": "synth.total_cell_area_um2",
      "channel": "intervals",
      "value":   { "min": 110000, "max": 160000, "unit": "um^2" },
      "horizon": "synth" },
    { "measurement_key": "synth.top_module_by_cells",
      "channel": "enum",
      "value":   ["multdiv","mult","alu","compressed_decoder","rvc"],
      "horizon": "synth" }
  ],

  "prevents": [
    { "downstream_stage": "pnr",
      "downstream_key":   "pnr.utilization_pct",
      "mechanism":        "Calibrated area expectation lets PnR floorplan choose die size correctly the first time.",
      "estimated_cost_saved_hours": 8 }
  ],

  "rationale": "Yosys+ABC and ORFS reference flows for Ibex/sky130hd consistently land in this range; the regfile and multiplier dominate cell count.",
  "citations": [
    { "title": "lowRISC/ibex syn README",
      "url":   "https://github.com/lowRISC/ibex/blob/master/syn/README.md" },
    { "title": "CARRV 2021 Ibex paper",
      "url":   "https://carrv.github.io/2021/papers/CARRV2021_paper_67_Schiavone.pdf" }
  ],
  "authored_by": "seed-pack",
  "authored_at": "2026-04-15T00:00:00Z",
  "examples":   { "violating": [], "compliant": [] },
  "history":    []
}
```

---

## 7. Pack-file shape

```jsonc
{
  "pack":        "vlsi",
  "version":     "1.0.0",
  "schema":      "kb-rule/v1",
  "description": "Causal + structural rules for digital design from RTL through synth signoff.",
  "stages":      ["rtl", "synth"],          // declared coverage; load-time validation
  "tools":       ["verilator", "slang", "yosys"],
  "key_index":   {                          // forward-declare measurement keys for validation
    "rtl.lint.LATCH.count":         { "type": "int",  "unit": "count" },
    "rtl.module.max_comb_depth":    { "type": "int",  "unit": "levels" },
    "synth.total_cell_area_um2":    { "type": "float","unit": "um^2" },
    "synth.top_module_by_cells":    { "type": "str",  "unit": null    },
    "sta.wns_ps":                   { "type": "float","unit": "ps"    }
  },
  "rules":       [ /* … */ ]
}
```

`key_index` is the load-time contract. If a rule predicts a key that
the pack hasn't declared, loading fails fast — far better than a stale
typo silently making every prediction "unfalsifiable".

---

## 8. Telemetry — append-only history

Every reflect-phase action appends one event:

```jsonc
{
  "at":           "2026-05-06T12:34:56Z",
  "event":        "cited",                  // cited | edit | retire
  "session_dir":  "runs/real/20260506_141938",
  "qid":          "q1_total_cells",
  "verdict":      "right_and_right_reason",
  "evidence_key": "synth.total_cell_area_um2",
  "evidence_value": 138420
}
```

`edit` events also carry `from_strength` / `to_strength` /
`reason` / `kind: strengthen|weaken|add_unless|narrow_when|broaden_when`.
`retire` events carry `reason` and a final-snapshot pointer.

Counters (`times_right`, `times_wrong`, `times_unfalsifiable`,
`last_failed_at`) are **derived on load**. Never stored.

This is the single biggest fidelity win over v0: KB diffs across
sessions tell a causal story about why a rule's strength moved, not
just where it landed.

---

## 9. What the format intentionally does NOT do

- **No rule priorities or conflict resolution.** Two rules disagreeing
  is signal for the predictor; resolving in the KB defeats the point.
- **No DSL.** Predicates are JSON. No parser, no new file extension.
- **No per-stage pack split.** One pack, many stages, filtered by
  `applies_to.stage`.
- **No automatic rule mining.** The reflect organ adds rules from
  *its own* surprises; we don't scrape lint logs into rules. Future
  scope.
- **No multi-language statements.** English only for v1. Localize
  later if anyone cares.
- **No probability distributions.** `strength` is a 3-bucket enum.
  Calibrating real probabilities needs scale we don't yet have.

---

## 10. Implementation order (when you say go)

1. JSON-schema file: `cogni/agent/kb_schema.json` (v1 schema, used at
   load time). Fail fast on unknown enums, missing required fields,
   undeclared measurement keys.
2. Loader: `agent/kb.py` — accept v0 and v1; auto-upgrade v0 → v1 in
   memory; validate against schema; derive counters from `history`.
3. Predicate evaluator: `agent/predicates.py` — ~50 lines, one
   function `evaluate(node, world, reality, target) -> bool`.
4. Update predictor prompt to read `predicts[*]` and emit
   `structured_claim` keyed by the listed `measurement_key`s.
5. Update reflect organ to write `history` events instead of
   rewriting `performance` in place.
6. Migrate `packs/vlsi/rules.json` and `packs/weather/rules.json` to
   v1 by hand (13 + 6 rules).
7. Tests:
   - one per predicate `op`
   - one per `kind` (constraint vs tendency grading)
   - one for `prevents` round-trip
   - one for `code_origin` filter (AI-RTL rule fires only when origin
     marked)
   - one for v0 → v1 auto-upgrade idempotence
   - end-to-end: a `prevents` rule with `horizon != applies_to.stage`
     gets correctly graded after the downstream stage runs.

Estimated: one focused day for steps 1–5, half a day for migration +
tests.

---

## 11. Defaults locked

To remove the four "open questions" from the prior draft and stop
debating:

1. **Predicate ops** — the 12-op list in §3 is final. No `between`
   (use `gte` + `lte`); no `regex_any` (use `any` of `matches`).
2. **Strength vocab** — `high | medium | low`. Mapped to
   `CERTAIN | CONFIDENT | LIKELY` in the predictor prompt.
3. **Citations** — objects `{title, url}`. Required.
4. **`design_class`** — open list with conventional values (§2.4).
   No closed enum. Adding new IPs shouldn't require a schema bump.

That's the format. Ship it.
