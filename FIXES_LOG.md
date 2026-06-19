# FIXES_LOG.md — v1 pack scenario iteration log

Each entry: `YYYY-MM-DD HH:MM | scenario | symptom | root cause | fix | files`

---

## 2026-05-06 — Phase 3b: scenario file authoring + adapter glue

### F-001 (rtl_demo): perceiver `from_findings_file` namespace check
- symptom: dry-run loaded fine on first try.
- fix: none needed; namespace allow-list already included `rtl.|sim.|synth.|sta.|power.|core.|target.|pdk.|pnr.|dft.`

### F-002 (ibex_synth): pack predicts `synth.ff_share_pct` / `synth.invbuf_share_pct` / `synth.warnings.latch` but YosysOracle didn't emit them
- symptom: questions q4/q7/q8 would have no measurement to compare against.
- root cause: derived shares + latch-warning count never wired into the oracle's measurement dict.
- fix: extended `adapters/synth/yosys/oracle.py` `from_existing` to compute `synth.ff_share_pct`, `synth.invbuf_share_pct`, `synth.icg_per_1kff`, `synth.warnings.latch` from the parsed gate histogram. Real-flow path (yosys.log warning lines) is still TODO; today this approximates from generic-netlist gate types.
- files: adapters/synth/yosys/oracle.py
- tests: 73/73 still green.

### F-003 (run_real): `cmd_prepare` only knew two scenario names
- symptom: rtl_demo and ibex_synth produced 0 attention briefs.
- root cause: hard-coded `if name == "ibex_signoff"` branch, no fallback.
- fix: collapsed the static-RTL branch to cover ibex_signoff, ibex_synth, rtl_demo (and any future scenario shipping questions.json + a perceiver/oracle pair). `rtl_root` is now optional — manifest-based perceivers don't need it.
- files: run_real.py

### F-004 (Perceiver): empty raw_inputs skipped adapter.perceive entirely
- symptom: rtl_demo manifest never loaded into the WorldModel.
- root cause: `Perceiver.perceive` looped over `raw_inputs` only; with empty list the manifest path never fired.
- fix: when `raw_inputs` is empty, call `adapter.perceive(world, "")` once so manifest-mode adapters can read their internal state.
- files: agent/perceiver.py

### F-005 (organs): predictor prompt f-string interpolated `{measurement_key, ...}` and `{min:0, max:0}` as Python names
- symptom: NameError on `measurement_key` blew up the predict stage on first call.
- root cause: single-brace literal dict examples inside an f-string.
- fix: doubled the braces (`{{...}}`) so they render literally.
- files: agent/organs.py
- tests: 73/73 still green.

### F-006 (organs): predictor refused all 5 rtl_demo questions because constraint rules predict `{{min:0,max:0}}` and the questions described violations
- symptom: all 5 outputs were `decision: refuse` with reason "rule says zero when compliant; question describes non-compliant code".
- root cause: prompt told the predictor that constraint rules MUST predict the compliant value, with no guidance on diagnostic/forensic counting questions.
- fix: rewrote the **Respect rule `kind`** section to explicitly cover the violation-count case: when the question or world describes concrete violations, count one per named instance and predict a NON-ZERO interval. Diagnostic questions on known-buggy RTL are now legitimate predictor territory.
- files: agent/organs.py
- tests: 73/73 still green.

### F-007 (Perceiver): synth-stage rules never fired on Ibex because no `synth_stage` tag
- symptom: ibex_synth ran with 0 candidates per question → all `no_output`.
- root cause: v1 synth pack `when` clauses gate on `tag: synth_stage`, but neither the IbexRTLAdapter nor the Perceiver wrapper added it. v0 packs used the Rule.stage field directly, so this never showed up before.
- fix: (1) `Perceiver.perceive` auto-adds `<adapter.stage>_stage` tag (covers all current and future stage adapters). (2) Added `stage = "synth"` and `tool = "yosys"` class attributes to IbexRTLAdapter so the auto-tag fires. (3) Added v1-pack discriminator tags to the Ibex adapter (`intended_flop_based`, `has_arith_or_indexing`, `stdcell_mapped`, `multicycle_multiplier`, `bus_fabric`, `mux_dominated`) so the matching rules fire.
- files: agent/perceiver.py, adapters/synth/yosys/perceiver.py
- before: 0/8 ibex_synth questions had candidates. after: 10 candidates per question fire.

### F-008 (kb): SCOPE edit crashed on v1 rules because `unless` is list-of-dicts, not list-of-strings
- symptom: finalize stage threw `TypeError: unhashable type: 'dict'` after reflect produced 4 SCOPE edits on v1 rules.
- root cause: legacy `apply()` used `sorted(set(r.unless) | set(edit.added_unless))` which assumes hashable string tags. v1 rules use predicate dicts.
- fix: branched on `r.schema_version`. v0 path unchanged. v1 path deduplicates by JSON-serialized representation and preserves insertion order. Strings in `added_unless` are auto-promoted to `{"op":"tag","name":<str>}` for compat.
- files: agent/kb.py
- tests: 73/73 still green.

## 2026-05-06 — Phase 3c: end-to-end run results (test mode)

**Session: runs/real/20260506_172150 — Sonnet + gpt-5-mini**

- rtl_demo: **3 right / 0 wrong / 2 refused / 0 KB edits**
  - q1 latch_count = 2 (right) | q3 blkseq = 2 (right) | q4 width = 1 (right)
  - q2 (case-incomplete) and q5 (downstream synth-latch-warnings) both honestly refused
    citing missing structural detail. Acceptable behavior — the predictor refused to
    invent counts beyond what the rules support.
- ibex_synth: **3 right / 2 wrong / 3 refused / 4 KB edits**
  - q2 top_module = ibex_multdiv_fast (right) | q5 lsu not in top 3 (right) | q8 latch_warnings = 0 (right)
  - q4 ff_share_pct: predicted ~11-14%, actual 6.26% (rule's published band 11-18% miscalibrated for this Ibex config). Reflector wrote SCOPE edits.
  - q7 invbuf_share_pct: predicted ~25%, actual 35.5% (similar calibration miss).
  - 3 refusals on q1/q3/q6 because Yosys generic-synth cell counts have no published band (the area-band rules are PDK-gated; without sky130hd/nangate45/asap7 we have nothing to ground the count to).

**Combined hit rate: 6/8 attempted = 75%, structured_claim_coverage 100%, cost $0.72.**

This is the v1 pack acceptance gate cleared: 73 tests + a real predict→verify→reflect→finalize loop on two scenarios.

## 2026-05-06 — Phase 4a: ensemble (N=3, test mode)

Three independent end-to-end runs of `run-all-api scenarios/rtl_demo scenarios/ibex_synth`
with `COGNI_TEST_MODE=1`, `--concurrency 3`. Sessions:

- `runs/real/20260506_172150` — cost $0.722, wall 873.7s
- `runs/real/20260506_173433` — cost $0.703, wall 717.2s
- `runs/real/20260506_174358` — cost $0.709, wall ~770s

**Cost**: mean $0.711, stdev $0.009, range $0.703-$0.722.
**Verdict variance**: zero. All 13 questions returned the same VerdictKind on every
run. The 6/2/5 right/wrong/refused split is reproducible.

| question | r1 | r2 | r3 |
|----------|----|----|----|
| rtl_demo::q1_latch_count       | right | right | right |
| rtl_demo::q2_case_incomplete   | refused | refused | refused |
| rtl_demo::q3_blkseq            | right | right | right |
| rtl_demo::q4_width_mismatch    | right | right | right |
| rtl_demo::q5_synth_latch_warn  | refused | refused | refused |
| ibex_synth::q1_total_cells     | refused | refused | refused |
| ibex_synth::q2_top_module      | right | right | right |
| ibex_synth::q3_total_ff        | refused | refused | refused |
| ibex_synth::q4_ff_share_pct    | wrong | wrong | wrong |
| ibex_synth::q5_lsu_in_top3     | right | right | right |
| ibex_synth::q6_top_gate_type   | refused | refused | refused |
| ibex_synth::q7_invbuf_share_pct| wrong | wrong | wrong |
| ibex_synth::q8_latch_warnings  | right | right | right |

The two wrongs (`q4_ff_share_pct`, `q7_invbuf_share_pct`) are calibration misses on
PDK-band rules — same root cause already documented for the Phase 3c run. The five
refusals are all "rule has no published band for this measurement"; the predictor
correctly refuses to invent numbers. None of the verdicts flipped run-to-run, which
is the result we wanted from this stage.

Aggregation script (for repro):

```python
import json, statistics
runs = ['20260506_172150','20260506_173433','20260506_174358']
agg = {}; costs = []
for ts in runs:
    s = json.load(open(f'runs/real/{ts}/summary.json'))
    costs.append(s['metrics']['cost_usd'])
    for sc, sd in s['scenarios'].items():
        for q in sd['questions']:
            agg.setdefault(f"{sc}::{q['id']}", []).append(q.get('verdict') or q.get('outcome'))
print('cost mean=%.3f stdev=%.3f' % (statistics.mean(costs), statistics.stdev(costs)))
print('stable:', sum(1 for vs in agg.values() if len(set(vs))==1), '/', len(agg))
```

## 2026-05-06 — Phase 4b/4c: deferred

**Phase 4b (single prod-mode high-water-mark run): deliberately skipped.**
With test-mode verdict variance at zero across N=3, the prod-mode run's main
contribution would be a one-shot quality ceiling. That's a useful number but
not a release blocker — and at ~5-8× test-mode cost ($3.50-5.50 per run) it's
not justified for this iteration. To run it later: `unset COGNI_TEST_MODE` and
follow the command in `docs/RUNBOOK.md` §4.

**Phase 4c (cost/speed audit): identified, not yet executed.**
Attention stage averages ~30k input tokens per run because every focused-rule
candidate carries its full citation block. Lowest-risk trim: in
`organs.attention_call`, project candidate rules to `{id, statement, kind,
strength, when, unless, predicts}` and drop `citations`/`rationale` from the
attention prompt only (keep them for predictor + reflector). Estimated savings
~30-40% of attention tokens, no expected verdict impact. Defer to next leg.

## 2026-05-06 — Phase 5: docs + audit

- `docs/AGENT_ARCHITECTURE.md` written (244 lines): cognitive loop, module map,
  data contracts, persistence layout, dispatcher abstraction, test surface.
- `docs/RUNBOOK.md` written (193 lines): setup, run commands, ensemble, prod
  mode, scenario authoring, adapter authoring, common failure modes.
- Workspace audit: no `oracles/` remnants, all `.py` compiles, no imports of
  removed modules. 73/73 tests still green.
- `README.md` already updated this leg with v1 pack section.

**Status:** v1 verdict layer complete and stable. Ready to consume more KB
content (more rules, more stages) without further structural change.
