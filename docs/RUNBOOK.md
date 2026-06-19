# Cogni — Runbook

Practical guide for running, debugging, and extending Cogni's verdict layer.
Read `docs/AGENT_ARCHITECTURE.md` first for the architecture; this doc is
operational.

## 0. Setup

```bash
cd /home/user/workspace/cogni
python3 -m pip install -r requirements.txt   # if a venv is in play
export ANTHROPIC_API_KEY=...                 # required for primary + verifiers
export OPENAI_API_KEY=...                    # required for the gpt verifier
# (optional) export GOOGLE_API_KEY=...        # only if a Gemini verifier is enabled
```

`COGNI_TEST_MODE=1` swaps the heavy models for cheaper ones:
- `claude_opus_4_7` → `claude_sonnet_4_6`
- `gpt-5` → `gpt-5-mini`

Use test-mode for everything except a deliberate high-water-mark run.

## 1. Run a Single Scenario

```bash
PYTHONPATH=. COGNI_TEST_MODE=1 python3 run_real.py \
    --concurrency 3 --test-mode \
    run-all-api scenarios/rtl_demo
```

Subcommands of `run_real.py`:
- `prepare <scenarios...>`     — perceive + observe + write pending_attention.json
- `attend-api <session_dir>`   — run attention LLM calls, write pending_predict.json
- `predict-api <session_dir>`  — run predictor calls, write pending_verify.json
- `verify-api <session_dir>`   — run verifier panel, write pending_reflect.json
- `reflect-api <session_dir>`  — run reflection, apply KB edits, write summary.json
- `finalize <session_dir>`     — re-derive summary.json from existing artifacts (no LLM calls)
- `run-all-api <scenarios...>` — convenience: prepare → attend → predict → verify → reflect

`run-all-api` is idempotent on the *prepare* boundary — re-running creates a new
timestamped session dir, so prior runs are preserved.

## 2. Run Both Stage Scenarios (the canonical sample)

```bash
PYTHONPATH=. COGNI_TEST_MODE=1 python3 run_real.py \
    --concurrency 3 --test-mode \
    run-all-api scenarios/rtl_demo scenarios/ibex_synth
```

Reference numbers (test-mode, N=3, May 2026):
- 13 questions across the two scenarios
- 6/13 right, 2/13 wrong, 5/13 honest refusals
- 100% structured_claim_coverage on the 8 committed predictions
- ~$0.71 / run (in: 175k tokens, out: 30k tokens)
- ~13 min wall-clock at concurrency 3
- **Verdict variance across N=3: zero** (all 13 questions returned the same VerdictKind every run)

## 3. Run an Ensemble (variance check)

```bash
PYTHONPATH=. python3 run_ensemble.py \
    scenarios/rtl_demo scenarios/ibex_synth \
    --n-runs 3 --concurrency 2 --test-mode --tag ensemble_v1
```

Each run lands in its own `runs/real/<ts>/` dir. The ensemble script does not
average verdicts — it just runs N copies. Aggregation is a one-shot Python
snippet (see `FIXES_LOG.md` Phase 4a entry for the exact script).

## 4. Production-Mode Run (high-water mark)

```bash
unset COGNI_TEST_MODE
PYTHONPATH=. python3 run_real.py --concurrency 2 \
    run-all-api scenarios/rtl_demo scenarios/ibex_synth
```

- Primary + reflector: `claude_opus_4_7`
- Verifier panel: `gpt-5` + `claude_opus_4_7`
- Expect ~5-8× test-mode cost. Use sparingly.

## 5. Tests

```bash
PYTHONPATH=. python3 -m pytest tests/ -q
# expected: 73 passed
```

The test suite uses `RecordReplayDispatcher` — no API calls, no rate limits.
If a real LLM call leaks into a test run, the dispatcher will raise.

## 6. Adding a Scenario

A scenario is a directory under `scenarios/` with these files:

```
scenarios/<name>/
    config.yaml         # stage, tool, pack_path, perceiver/oracle paths, confidence_floor
    manifest.json       # facts/tags fed to the perceiver (or rtl_root for live RTL)
    findings.json       # precomputed reality (bypasses tool execution)
    questions.json      # list of {id, question, expected_key, expected_value} entries
```

Minimum viable `config.yaml`:
```yaml
stage: rtl
tool: verilator
pack_path: packs/rtl/rules.json
perceiver:
  manifest_path: scenarios/<name>/manifest.json
oracle:
  findings_path: scenarios/<name>/findings.json
confidence_floor: uncertain
```

Questions follow the schema in `run_real.cmd_prepare` — each must include
`expected_key` (a `measurement_key`) for the verdict scorer to score it.

## 7. Adding a Stage / Tool Adapter

1. Create `adapters/<stage>/<tool>/perceiver.py`:
   ```python
   class Perceiver:
       stage = "<stage>"
       tool  = "<tool>"
       def perceive(self, raw_input: str) -> WorldModel: ...
   ```
2. Create `adapters/<stage>/<tool>/oracle.py`:
   ```python
   class Oracle:
       def observe(self, world: WorldModel, findings_path: str | None = None) -> Reality: ...
   ```
3. Make sure every measurement the oracle reports lives in the
   `<stage>.*` namespace (`rtl.*`, `synth.*`, `pnr.*`, `cts.*`, `sta.*`, …).
4. Wire the adapter selector in `agent/perceiver.py` (it currently dispatches
   on `(stage, tool)` from `config.yaml`).

## 8. Editing a Pack

Packs live at `packs/<stage>/rules.json`. Validate with:

```bash
PYTHONPATH=. python3 -c "
from agent.kb import KnowledgeBase
kb = KnowledgeBase.load('packs/rtl/rules.json')
print(f'rules: {len(kb.rules)}, key_index keys: {len(kb.key_index)}, version: {kb.version}')
"
```

The loader will reject any rule that fails the v1 schema. v0-style packs are
auto-upgraded in memory but not written back unless you call `kb.save()`.

For the canonical rule shape, see `docs/KB_RULE_FORMAT.md`. Closed enums:
- `kind`         : `constraint | tendency | heuristic | identity`
- `strength`     : `high | medium | low`
- `stage`        : `pre_rtl | rtl | synth | pnr | cts | sta | sim | power | signoff | weather`
- `code_origin`  : `any | human | ai_generated | ai_assisted | legacy_imported`
- `predicate.kind`: `eq | neq | lt | lte | gt | gte | in | not_in | has_tag | missing_tag | between`

## 9. Common Failure Modes

| Symptom | First check | Usual fix |
|---------|-------------|-----------|
| `0 candidates` for a question | recall stage filter + tag set on the world model | add the right adapter discriminator tag (see F-007) |
| `refused_by_primary` on a question that *should* answer | predictor prompt + missing_evidence in refusal record | confirm the cited rule has a `predicts[*]` for the asked key |
| `wrong_and_wrong_reason` with otherwise-correct rule | check the rule's `predicts[*]` interval against reality | rule needs calibration — propose `weaken_rule` edit, or split with `unless` |
| `Anthropic rate_limit_error 429` | concurrency too high for combined scenario | drop to `--concurrency 2` for combined runs (30k tok/min cap) |
| `0 questions ran` | `cmd_prepare` did not see your scenario type | extend the static-RTL branch in `run_real.cmd_prepare` (see F-003) |
| `NameError: measurement_key` in organs | f-string brace not escaped | double-brace any literal `{...}` in prompts (F-005) |
| `apply()` raises on `unless` list | v1 list-of-dict semantics | already fixed in `kb.apply` SCOPE branch (F-008) — make sure you're on current `agent/kb.py` |

## 10. Cost & Latency Knobs

- Concurrency: `--concurrency N` on `run_real.py`. Combined rtl_demo + ibex_synth
  hits Anthropic's 30k-tok/min input limit at N=4. N=3 is the safe ceiling.
- Attention prompt size: this is the largest input contributor. Trimming
  `world.facts` to the keys the rule's `when`/`unless`/`predicts` actually
  reference is the obvious next optimization (Phase 4c).
- Verifier panel size: each verifier ≈ 5-7k input tokens. Going from 2→1
  verifier roughly halves verify-stage cost but loses dissent-driven revision.

## 11. Where to Look First When Something Breaks

1. `runs/real/<latest>/summary.json` — high-level outcome per question.
2. `runs/real/<latest>/llm_calls/<call_id>.json` — the exact prompt + raw
   response for the failing call.
3. `runs/real/<latest>/refusals.jsonl` — `missing_evidence` is usually the
   most informative field.
4. `runs/real/<latest>/<scenario>_world.json` and `<scenario>_reality.json`
   — confirm the perceiver and oracle are reporting what you think.
5. `FIXES_LOG.md` — every non-trivial bug we've already hit, with the patch.
