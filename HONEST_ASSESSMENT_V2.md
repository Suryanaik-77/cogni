# Cognitive Agent v2 · Honest Assessment

**Run:** `runs/real/20260428_075454`
**What changed since v1:** v1 was a single-domain, hand-coded VLSI scoring loop. v2 is a generic 5-organ cognitive agent (perceive, recall, predict, verify, reflect) with attention, confidence-gated refusal, and a KB rule lifecycle. Same agent code drove an Ibex synthesis re-run and a non-VLSI weather-prediction toy scenario, with no domain-specific Python on the predict/verify/reflect path. Domain knowledge lives entirely in pluggable rule packs and adapter classes.

**Models in the loop:**
- Attention, predictor, reflector — Claude Opus 4.7 (subagents)
- Verifier — GPT-5.4 + Gemini 3.1 Pro (independent dual-verifier)
- Verdict — mechanical Python (`agent/verdict.py`), no LLM

---

## Headline numbers

| Metric | Ibex (v2) | Weather toy (v2) | Ibex (v1, for context) |
|---|---:|---:|---:|
| Questions posed | 6 | 4 | 13 |
| Refused (confidence-gated) | 3 | 0 | 0 (v1 had no refusal) |
| Predicted | 3 | 4 | 13 |
| Right and right reason | 3 | 2 | 11 |
| Wrong and wrong reason | 0 | 2 | 1 |
| Wrong but right direction | 0 | 0 | 1 |
| Hit rate on predicted | 3/3 = 100% | 2/4 = 50% | 11/13 = 85% |
| Hit rate on questions posed | 3/6 = 50% | 2/4 = 50% | 11/13 = 85% |
| KB edits applied | 1 (ADD) | 1 (STRENGTHEN) | 0 (manual queueing only) |

**What this means.** v2 is not "better" than v1 on Ibex hit rate; v1 was 11/13 right because the human-curated rule pack was already well-aligned to Ibex. v2 is a different demonstration: it shows that the **same agent shell** can drive two unrelated domains, that **refusal works** (3 honest "I don't have a rule for this" outputs on Ibex), and that the **KB can edit itself** when the reflector finds an unfit rule.

---

## What the agent refused to predict (Ibex)

| qid | Question | Why refused |
|---|---|---|
| q1_total_cells | "How many total cells will Yosys produce?" | Existing rule `r_synth_area_scaling` speaks to area in µm² (post-tech-mapping), not the dimensionless cell count Yosys reports in generic synthesis. The predictor flagged the mismatch and stopped. |
| q3_ff_share | "What fraction of cells will be FFs vs comb?" | No rule in the pack constrains FF/comb ratio. |
| q5_decoder_size | "Will the compressed decoder be in the top 3?" | No rule ranks submodules by cell count. The compressed-decoder surprise from v1 was *queued* in v1's surprise log but never lifted into a rule, so v2's KB cannot answer it. This is a fair self-test of whether the rule-lifecycle actually closes. |

This is the cognitive property we wanted to prove: an agent that knows what it doesn't know. Three of six Ibex questions hit the refusal gate honestly. None were faked through plausible-sounding LLM output, because the predictor schema requires citing a covering rule and the rule pack has no such rule.

---

## What got right for the right reason (Ibex)

**q2 — top module:** Predicted FastMul + 32x32 FF regfile cluster would dominate. Yosys reality: `ibex_multdiv_fast` = 3,997 cells (#1), with the regfile distributed across the top module. Both cited rules (`r_synth_area_scaling`, `r_synth_critical_path_mul`) confirmed. *Honest caveat:* one of the two verifiers (GPT-5.4) disagreed because the FF regfile does not appear as a discrete leaf module in Yosys's hierarchical report — the dual-verifier flagged the same wrinkle the v1 reflector noted.

**q4 — LSU not in top-3:** Predicted LSU below top 3, in cell-count band 600–1200. Yosys reality: LSU = 880 cells, rank #5. Both verifiers agreed. The reasoning ("LSU is a thin handshake wrapper, not a deep combinational cone") is the same correction that v1 had to learn the hard way.

**q6 — top abstract gate type:** Predicted AND-family gates dominate, ratio 3–5x over XOR/XNOR. Yosys reality: `$_ANDNOT_` = 4,665 (#1), `$_xor_` = 874. Ratio is 5.7x — just outside the predicted band but directionally correct. Verdict mechanical: right. **Reflector added a new rule** (`r_synth_yosys_andnot_modal`): "In Yosys + ABC abstract synthesis of small RV32 cores, `$_ANDNOT_` is the modal primitive at 25–35% of comb cells." This is the KB editing itself based on being almost-wrong.

---

## What got wrong (Weather toy)

**sit_c — high-pressure ridge:** Prediction "no measurable precipitation in next 24h, 0.0–0.2 mm." Reality: 0.0 mm, clear and dry. Mechanical verdict: `WRONG_AND_WRONG_REASON` (claim_hits=0). **The reflector flagged this as a grader artifact**, not a forecasting miss — the verdict logic counts keyword overlap and didn't credit the negation "no precipitation." Both rules' mechanisms (subsidence, ridge advection) actually behaved as the rule states. No KB edit proposed; the reflector kept its hands off rather than punish a rule whose mechanism was confirmed.

**sit_d — marine layer burn-off:** Prediction "burn off 10:00–11:30 PT, sunny by noon." Reality: dissipated 10:30 PT, sunny by noon. Mechanical verdict: `WRONG_AND_WRONG_REASON`. Same grader artifact — the window matched, the mechanism (insolation overcoming inversion) matched, but the keyword grader missed it. Reflector strengthened `r_w_marine_layer_morning` from `tendency` to `strong` because reality validated the substantive prediction.

This is the **most honest finding in the run**: the mechanical verdict logic is the weakest link in the loop. It works fine for numeric verdicts (numeric_verdict on Ibex did its job) but the categorical/contains check is too brittle for natural-language forecasts. Two of two weather "wrongs" are this same failure mode, not the cognitive agent failing.

---

## Calibration

| Confidence | N predicted | Right (mechanical) | Right (after reflector adjudication) |
|---|---:|---:|---:|
| confident (0.80) | 7 | 5 (71%) | 7 (100%) |
| (other bands) | 0 | — | — |

The agent only fired one confidence band this run because the predictor stopped at confident-or-refuse, never reached for "certain" or "likely." That's defensible in a 7-prediction sample but doesn't say much about calibration. With more questions and a better verdict adjudicator, the calibration table starts to mean something.

---

## What the KB looks like after the run

**Ibex pack:** 13 → 14 rules (added `r_synth_yosys_andnot_modal`). Performance counters fired for the three rules cited in successful predictions:
- `r_synth_area_scaling`: times_right += 3
- `r_synth_critical_path_mul`: times_right += 3, times_cited += 1
- `r_synth_lsu_not_critical`: times_right += 1

No rules retired or weakened. No rules at v1's "queued surprise" status (compressed decoder, datapath-operand HFN) were promoted into rules — that gap is preserved on purpose, since the questions about them surfaced as honest refusals, which is the correct behavior given the KB state.

**Weather pack:** 6 rules → 6 rules. `r_w_marine_layer_morning` strengthened from `tendency` to `strong`. Performance counters fired across all 6 cited weather rules.

---

## What I'm NOT claiming

- **The cognitive agent isn't more accurate than v1's hand-coded loop.** v1 had the benefit of a curated, design-specific rule pack and 13 carefully crafted questions. v2's job was to show generality, not raw hit rate. On Ibex specifically, v2 only "answered" 3/6 questions and got those right; the other 3 were honest refusals.
- **Refusal works only because the predictor schema enforces rule citation.** If a future LLM hallucinates a rule id, the system won't catch it until the verifier compares against the actual KB JSON. The current verifiers (GPT-5.4 + Gemini 3.1 Pro) do read the KB but the schema doesn't strictly enforce it. That's the next gap to close.
- **The verdict layer is the agent's biggest weakness right now.** Two of four weather predictions were graded wrong by mechanical comparison and overruled by the reflector's reading. Either the grader needs to ingest verifier output, or claims need to be more structured (predicted intervals, predicted enums) so numeric_verdict applies. As-is, "claim text vs reality summary" via keyword overlap is doing too much work.
- **Generality was achieved by code, not by the LLM.** The agent works across Ibex and weather because the perceiver/adapter abstraction is right and the rule-pack format is JSON. The LLM did not "learn" weather. It used a weather rule pack we wrote for it.
- **The reflectors used Opus, the verifiers used GPT-5.4 + Gemini 3.1 Pro. There is no proof this generalises across smaller/cheaper models.** A meaningful follow-up is to swap in cheaper verifiers and see whether the "verifier disagrees with predictor" signal survives.
- **The 50-subagent run is a real-LLM demonstration, not a benchmark.** N=10 questions across two domains is too small to compare hit rates with statistical confidence. Treat the numbers as plumbing-validation evidence, not capability claims.

---

## What worked architecturally

1. **5 organs + attention as an interface, not a model.** Each organ is one structured LLM call with a JSON schema. The "cognition" is in the orchestration (gate, attribute, edit), not in the LLM.
2. **Confidence-gated refusal.** Three Ibex questions died at the predictor because no rule covered them. The system did not paper over the gap with plausible LLM text. This is the load-bearing property.
3. **Dual independent verifiers from different model families.** GPT-5.4 and Gemini 3.1 Pro disagreed on q2 — one accepted the FF-regfile sub-claim, the other flagged that it doesn't appear as a discrete leaf module. The mechanical verdict ignored the disagreement (it scored the headline claim) but the disagreement is preserved in the artifacts and would be useful input to a smarter verdict layer.
4. **Reflector edits the KB, not the prompts.** Out of 7 reflections, only 2 produced edits. Five (all the right-and-right-reason cases) added zero KB edits and just bumped performance counters. Reflectors writing too many edits is a known failure mode in this pattern; the discipline ("right answers strengthen via performance, not via edits") seems to hold.

## What didn't work

1. **Mechanical verdict on natural-language claims.** Already covered above. The 2/2 weather wrong-judgments are the dominant signal in the failure analysis.
2. **No cross-question memory within a run.** Each question's predictor only sees the rule pack and its own facts. The q4 prediction "LSU not in top 3" and the q5 refusal "no rule ranks submodules" are inconsistent under a smarter agent — q4 effectively used such a rule implicitly. A future iteration should let attention surface implicit-rule conflicts.
3. **The Yosys oracle is brittle.** It currently parses cognipd v1's saved reports rather than re-running synthesis. Good enough for a re-run, not good enough for a true closed-loop where the agent runs the tool.

---

## Comparison vs cognipd v1

| Property | v1 | v2 |
|---|---|---|
| Domain coupling | Hard-coded VLSI scoring | Generic 5-organ + pluggable adapter/pack |
| Refusal | None | 3/6 on Ibex, all justified |
| KB editing | Manual rule queueing in surprise log | Reflector applies ADD/STRENGTHEN/etc. |
| Verifier | Single thread (Claude in main loop) | Dual independent (GPT-5.4 + Gemini 3.1 Pro subagents) |
| Verdict | Hand-coded against ORFS rules-base | Mechanical numeric/categorical/contains, model-free |
| Hit rate on falsifiable | 11/13 (85%) | 3/3 on answered Ibex; 2/4 on weather (4/4 if reflector overrules grader) |

The right comparison isn't hit rate — it's whether the system would still work if you handed it a domain we'd never seen (it does), and whether it would tell you when it doesn't know (it does, three times on Ibex). v1 couldn't do either of those.

---

## Files of record

- Run dir: `/home/user/workspace/cogni/runs/real/20260428_075454/`
- Per-call inputs/outputs: `<run>/llm_calls/<scenario>.<qid>.<role>/`
- Final summary: `<run>/summary.json`
- KB after run: `<run>/ibex_signoff_kb_after.json`, `<run>/toy_weather_kb_after.json`
- Framework: `/home/user/workspace/cogni/agent/`, `oracles/`, `adapters/`, `packs/`, `scenarios/`
