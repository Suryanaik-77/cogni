# Cogni

**Cognition-based stage gate for chip design.**

You give Cogni an RTL tree or a synth netlist; it sweeps the design
through a knowledge pack of 60 rules and produces a list of issues to
fix before moving to the next stage. With `--propose-fixes` it also
generates patches via a cross-LLM cognitive loop (predict → verify →
revise → reflect).

```bash
# Stage-gate RTL: list every issue
python3 cogni_check.py path/to/my_chip --stage rtl

# Stage-gate synth + propose patches for every violation
python3 cogni_check.py path/to/my_chip --stage synth --propose-fixes
```

Outputs `REPORT.md` (human), `REPORT.json` (machine), and one
`.patch` per accepted fix.

## Why this exists

Standard linters tell you *what* the tool found. They don't tell you
*what to do about it* in your house style, and they don't reason about
the design as a whole. Cogni operates one layer up: a knowledge pack
encodes design rules with `predicts[*]` measurement bands plus
compliant code examples, the runtime sweeps them against tool output,
and a cognitive layer proposes minimal patches that match the rule's
compliant pattern. Every patch goes through cross-LLM verification
before it's accepted, so refusals are honest and accepted patches went
through a peer-review loop.

## What's in the box

| Layer | Files |
|-------|-------|
| Knowledge packs | `packs/rtl/rules.json` (34 rules) + `packs/synth/rules.json` (26 rules) |
| Tool runners | `adapters/rtl/verilator/`, `adapters/synth/yosys/` |
| Sweep engine (deterministic) | `agent/sweep.py` |
| Fix proposer (cognitive) | `agent/fixer.py` |
| Cognitive primitives | `agent/organs.py`, `agent/orchestrator.py`, `agent/kb.py`, `agent/predicates.py`, `agent/verdict.py` |
| LLM transports | `agent/llm/` — Claude + OpenAI + Gemini |
| Report writers | `agent/report.py` |
| CLI | `cogni_check.py` |
| Tests | `tests/` — 92 tests, no API calls |

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit with your API keys

# Verify
PYTHONPATH=. python3 -m pytest tests/ -q
# expected: 92 passed

# Sweep only (no API keys needed)
PYTHONPATH=. python3 cogni_check.py scenarios/rtl_demo --stage rtl

# Sweep + cognitive fix proposer (uses keys, ~$0.20 in test mode)
PYTHONPATH=. python3 cogni_check.py scenarios/rtl_demo \
    --stage rtl --propose-fixes --test-mode
```

Full setup guide in [`INSTALL.md`](INSTALL.md).

## How a check runs

```
Your RTL / netlist
    │
    ▼
1. Tool runner adapter (verilator / yosys / findings.json replay)
    │         emits measurements in rtl.* / synth.* namespace
    ▼
2. Sweep engine  ───  pure Python, no LLM
    │         classifies each rule: violation | clean | skipped | n/a
    │         PDK rules auto-skip if pdk.* tag is absent
    ▼
3. (optional) Fix proposer per violation:
    a. Propose patch        (Claude)
    b. Verify panel         (GPT + Gemini, parallel)
    c. Revise if any dissent (Claude)
    d. Reflect on outcome   (Claude — may propose KB edits)
    ▼
4. Report writer
    REPORT.md  +  REPORT.json  +  patches/<rule_id>__<file>.patch
```

## Sample numbers

`scenarios/rtl_demo` (34 RTL rules, a 150-line cmd_alu with deliberate
hazards):

- **Sweep**: 8 violations, 14 clean, 2 PDK-skipped, 10 n/a — ~1s
- **Sweep + fix proposer (test mode)**: 8 patches in ~130s, ~$0.20
- Verifier panel rejected 6 of the initial 8 patches — revision loop
  produced the final accepted versions

`scenarios/ibex_synth` (26 synth rules, against Yosys+Sky130 reports
for the Ibex core):

- **Sweep**: 2 violations, 3 clean, 16 PDK-skipped (PDK band rules
  correctly gated), 5 n/a — ~1s
- **Sweep + fix proposer**: both refused honestly — proposer couldn't
  produce a surgical patch without source RTL

The verdict-layer ensemble at the bottom of `FIXES_LOG.md` shows
test-mode runs have **zero verdict variance across N=3** at $0.71/run.

## Knowledge pack format

A pack is a v1 JSON document of rules. Each rule looks like:

```json
{
  "id": "r_rtl_comb_total_assignment",
  "kind": "constraint",
  "strength": "high",
  "applies_to": {"stage": ["rtl"]},
  "statement": "Every signal in always_comb must be assigned on every path.",
  "when":   [{"op": "tag", "name": "rtl_stage"}],
  "unless": [],
  "predicts": [
    {"measurement_key": "rtl.lint.latch.count",
     "channel": "intervals", "value": {"min": 0, "max": 0}}
  ],
  "examples": {
    "compliant":   ["always_comb begin\n  y = '0;\n  if (sel) y = a;\nend"],
    "violating":   ["always_comb begin\n  if (sel) y = a;\nend"]
  },
  "citations": [
    {"title": "Verilator warnings",
     "url": "https://verilator.org/guide/latest/warnings.html"}
  ]
}
```

Closed enums: `kind = constraint | tendency | heuristic | identity`,
`strength = high | medium | low`, channel = `intervals | enum |
includes | excludes`. See [`docs/KB_RULE_FORMAT.md`](docs/KB_RULE_FORMAT.md)
for the full reference (579 lines, every field, every enum, all
gotchas).

## Adding a stage

The runtime is EDA-tool-agnostic. To support a new stage (PnR, CTS,
STA, …) you add:

```
adapters/<stage>/<tool>/
    perceiver.py     # raw_input -> WorldModel (facts + tags)
    oracle.py        # observe -> measurements dict in <stage>.* namespace
    runner.py        # (optional) live tool invocation
packs/<stage>/rules.json
```

Then `cogni_check.py --stage <stage> ...` just works. See the existing
rtl / synth adapters as templates.

## Docs

| File | What it covers |
|------|-----|
| [`INSTALL.md`](INSTALL.md) | Setup, .env, first run |
| [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) | Cognitive loop, module map, data contracts, persistence layout |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Day-to-day commands, scenario authoring, adapter authoring, failure modes |
| [`docs/KB_RULE_FORMAT.md`](docs/KB_RULE_FORMAT.md) | Full v1 rule format reference |
| [`FIXES_LOG.md`](FIXES_LOG.md) | Every non-trivial bug we hit + the patch |

## Status

92 tests passing. Two end-to-end scenarios green. Verdict variance
zero across N=3 in test mode. Ready to absorb more rules and more
stages without structural change.

## Non-goals (deliberate)

- **No RTL auto-application.** Patches stay in `patches/`. You apply
  with `git apply` after review.
- **No probability distributions.** Strength is a 3-level enum,
  confidence is 4-level.
- **No rule-mining from lint output.** Reflection proposes KB edits
  via LLM reasoning, not pattern extraction.
- **English-only for v1.**
