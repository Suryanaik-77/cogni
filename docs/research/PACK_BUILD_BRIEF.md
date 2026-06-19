# Brief: convert dossier → kb-rule/v1 JSON pack

This is the build brief for the rule-pack-builder subagents. Two packs:

- RTL pack: read `docs/research/RTL_RULES_RESEARCH.md` (34 rules, IDs R-01..R-34) → write `packs/rtl/rules.json`.
- Synth pack: read `docs/research/SYNTH_RULES_RESEARCH.md` (24 rules, IDs R-01..R-24) → write `packs/synth/rules.json`.

## Authoritative format

The rule object shape, closed enums, predicate language, measurement
namespace, and verdict channels are defined in `docs/KB_RULE_FORMAT.md`
(read it end-to-end). The JSON Schema at `agent/kb_schema.json` is the
final tiebreaker — packs MUST validate against that schema with
`jsonschema` Draft 2020-12. Run validation before finalizing.

## Pack envelope (top of file)

```json
{
  "pack":    "rtl"  /* or "synth" */,
  "version": "1.0.0",
  "schema":  "kb-rule/v1",
  "description": "<one-sentence pack purpose>",
  "stages":  ["rtl"]  /* or ["synth"] for synth pack; for cross-stage RTL rules with synth horizon, list ["rtl","synth"] */,
  "tools":   ["verilator","slang", ...]  /* synth: ["yosys","abc","openroad","dc","genus", ...] */,
  "key_index": { /* see below */ },
  "rules": [ ... ]
}
```

## key_index: forward-declare every measurement key your rules predict

For every key referenced in any rule's `predicts[*].measurement_key` OR
`prevents[*].downstream_key`, add an entry:

```json
"key_index": {
  "rtl.lint.LATCH.count":  { "type": "int",   "unit": "count",  "description": "Inferred-latch count from Verilator/slang lint" },
  "synth.warnings.LATCH":  { "type": "int",   "unit": "count",  "description": "Yosys LATCH warning count" },
  "synth.total_cell_area_um2": { "type": "float", "unit": "um2", "description": "Sum of stdcell areas after synth" }
}
```

The schema enforces `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$` for keys.
Use the prefixes from KB_RULE_FORMAT.md §4: `rtl.*`, `synth.*`, `pnr.*`,
`cts.*`, `sta.*`, `sim.*`, `power.*`, `core.*`, `target.*`, `pdk.*`.

## Per-rule construction recipe

For each dossier rule, produce ONE JSON object with these fields:

1. **identity**
   - `id`: `"r_rtl_<short>"` for RTL pack, `"r_synth_<short>"` for synth.
     Use a slug derived from the dossier's section header (e.g. `r_rtl_comb_total_assignment`).
   - `version`: `1` (integer)
   - `statement`: copy the dossier's statement, trim to ~1-2 sentences.
   - `kind`: from dossier's `kind:` field — must be one of `constraint | tendency | heuristic | identity`.
   - `strength`: from dossier's `strength:` — must be `high | medium | low`.
   - `status`: `"active"`.

2. **applies_to**
   - `stage`: closed enum `[pre_rtl|rtl|synth|pnr|cts|sta|sim|power|signoff|weather]`. RTL pack rules: `["rtl"]` baseline. If the rule predicts something gradable only at synth (i.e. `predicts[].horizon == "synth"`), list `["rtl","synth"]`.
   - `tools`: list — RTL pack: `["verilator","slang"]` for lint-style rules, `["any"]` for tool-agnostic. Synth pack: derive from dossier's `applies_to.tools:` line.
   - `pdks`: derive from dossier; default `[]` (any).
   - `design_class`: derive from dossier; default `["any"]`.
   - `code_origin`: derive from dossier's `applies_to.code_origin:` line; default `["any"]`.

3. **when / unless** (predicate trees)
   - Translate dossier's `when:` and `unless:` text into `[{op,...}]` nodes.
   - Always include `{"op":"tag","name":"<stage>_stage"}` as the first `when` entry. (e.g. `rtl_stage`, `synth_stage`.)
   - If the dossier mentions a numeric threshold ("max combinational depth > 12"), use `{"op":"gt","key":"rtl.module.max_comb_depth","value":12}`.
   - For categorical predicates ("AI-generated"), use `{"op":"in","key":"core.code_origin","values":["ai_generated","ai_assisted"]}`.
   - For "block exists" patterns, use `{"op":"exists","key":"rtl.always_comb_blocks"}`.
   - When there's no specific gating beyond the stage tag, leave `unless: []`.

4. **predicts** (the falsifiable claim)
   - One entry per measurement-claim. From dossier's `predicts: [(key, channel, value), ...]` line.
   - `measurement_key`: the dotted key. Be careful with namespace — dossier may say `synth.area` but use `synth.total_cell_area_um2` to match the existing oracle.
   - `channel`: `intervals | enum | ranking | includes | excludes`. For `kind=constraint` with risk=lower / count=0, use `intervals` with `{min:0,max:0}`.
   - `value`: shape per channel (see KB_RULE_FORMAT.md §5).
     - intervals: `{"min":N,"max":M,"unit":"..."}` (or `{"min":0,"max":0}` for "must be zero")
     - enum: `[...choices...]`
     - ranking: ordered list of names
     - For "direction up/down/lower" claims that don't have a numeric anchor, use `intervals` with a wide range (e.g. `{"min":0,"max":1000000}`) and document it in `rationale`. Or skip the predict entry — better to have fewer high-quality predicts.
   - `horizon`: stage at which this becomes gradable. Critical for shift-left: an RTL rule predicting `synth.warnings.LATCH` has `horizon: "synth"`.

5. **prevents** (the shift-left linkage)
   - One entry per downstream issue this rule heads off. From dossier's `prevents:` line.
   - `downstream_stage`: which stage the bug would otherwise show up at.
   - `downstream_key`: the measurement key that would change.
   - `mechanism`: one-sentence why-this-prevents-that.
   - `estimated_cost_saved_hours`: numeric (use median of dossier's range, e.g. "4-24 engineer-hours" → 14).

6. **rationale**: copy dossier's `rationale:` paragraph, trim to ~3 sentences.

7. **citations**: dossier's `citations:` semicolon-list → array of `{"title":"...","url":"..."}` objects. **Required** by schema. Include 2-4 high-quality URLs per rule.

8. **examples**: include `{"violating":[...], "compliant":[...]}` only when the dossier provides clear code snippets or you can derive them safely. Optional. Skip if you don't have anchored snippets — DO NOT fabricate Verilog.

9. **authored_by**: `"seed-pack-rtl"` or `"seed-pack-synth"`.
   **authored_at**: `"2026-05-06T00:00:00Z"`.

10. **history**: `[]` (empty; agent appends events at runtime).

## Validation step (mandatory before declaring done)

After writing the JSON, run:

```bash
cd /home/user/workspace/cogni
python3 -c "
import json, jsonschema
with open('agent/kb_schema.json') as f: schema = json.load(f)
with open('packs/rtl/rules.json') as f: pack = json.load(f)   # or packs/synth
jsonschema.validate(pack, schema)
print('OK', pack['pack'], 'rules:', len(pack['rules']))
"
```

If validation fails, fix and re-run until it passes.

## Final acceptance check

After validation, run the existing loader to make sure cogni accepts the pack:

```bash
cd /home/user/workspace/cogni
python3 -c "
from agent.kb import KnowledgeBase
kb = KnowledgeBase.load('packs/rtl/rules.json')   # or packs/synth
print('loaded', len(kb.rules), 'rules')
for r in kb.rules[:3]:
    print(' -', r.id, r.kind, r.strength.name)
"
```

Both must print successfully. Report the final rule count and any rules
you had to drop or simplify.
