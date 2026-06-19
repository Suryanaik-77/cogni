# Cogni — install & first run

This is the get-it-running-on-your-laptop guide. ~5 minutes if your
Python is healthy. For architecture see `docs/AGENT_ARCHITECTURE.md`,
for command reference see `docs/RUNBOOK.md`.

## 0. Prerequisites

| Thing | Version | Notes |
|-------|---------|-------|
| Python | 3.10+ | needed for PEP-604 union types |
| pip / venv | standard | |
| git | optional | only if you cloned the repo |
| Verilator | 5.x | optional — only for live RTL linting (replay works without) |
| Yosys | 0.39+ | optional — only for live synth (replay works without) |

The CLI runs **without** Verilator/Yosys installed by replaying
precomputed `findings.json` / `reports/` files. You only need the EDA
tools when you point Cogni at fresh RTL or a fresh netlist.

## 1. Get the code

Two options.

### Option A: copy the workspace

If you received the project as a folder, just `cd` into it.

### Option B: from a tarball

```bash
tar xzf cogni.tar.gz
cd cogni
```

## 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

This pulls in:

- `anthropic`, `openai`, `google-genai` — the three LLM SDKs the fix
  proposer talks to in parallel.
- `python-dotenv` — for loading `.env`.
- `PyYAML` — optional, scenario configs.
- `pytest` — for the test suite.

## 3. API keys

Create a `.env` file in the project root:

```bash
cat > .env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...
EOF
chmod 600 .env
```

All three keys are required for `--propose-fixes` (the verifier panel
uses GPT + Gemini, the proposer uses Claude). For sweep-only runs (no
fixes), no keys are needed.

Test mode (`--test-mode`) routes the same architecture through cheaper
models (`claude-sonnet`, `gpt-5-mini`, `gemini-flash`). Use it for
everything except the deliberate high-water-mark run.

## 4. Verify the install

```bash
PYTHONPATH=. python3 -m pytest tests/ -q
```

Expected output: `92 passed in ~0.1s`. No API calls; all tests use
recorded fixtures.

## 5. First run — RTL sweep only

```bash
PYTHONPATH=. python3 cogni_check.py scenarios/rtl_demo --stage rtl
```

Expected: a report at `scenarios/rtl_demo/report-<timestamp>/`
containing `REPORT.md` + `REPORT.json`. No API keys consumed. Sample
output:

```
[cogni-check] stage=rtl  pack=packs/rtl/rules.json
[cogni-check] reality source=findings_json
[cogni-check] measurements: 33  world facts: 29  tags: 7
[cogni-check] 34 rules: 8 violations, 14 clean, 2 skipped, 10 n/a
```

Open `REPORT.md` in any markdown viewer to see the per-rule analysis.

## 6. First run — RTL with fix proposals (LLM cost ~$0.10-0.20 in test mode)

```bash
PYTHONPATH=. python3 cogni_check.py scenarios/rtl_demo \
    --stage rtl --propose-fixes --test-mode --concurrency 4
```

This adds the cognitive layer (Claude proposer + GPT/Gemini verifier
panel + revision on dissent + Claude reflector). Expected wall-clock:
~2 min for 8 violations. Output now also includes `patches/` with one
`.patch` file per accepted fix.

## 7. First run — synth stage

```bash
PYTHONPATH=. python3 cogni_check.py scenarios/ibex_synth --stage synth
PYTHONPATH=. python3 cogni_check.py scenarios/ibex_synth --stage synth --propose-fixes --test-mode
```

The synth scenario points at a Yosys reports directory. PDK-band rules
are silently skipped because the scenario doesn't supply a `pdk.name`
tag — that's the gating behaviour, working correctly.

## 8. Pointing Cogni at your own design

### RTL stage

```bash
PYTHONPATH=. python3 cogni_check.py \
    --stage rtl \
    --rtl-root path/to/your/rtl \
    --top-module my_top \
    --out path/to/where/report/should/land
```

Requires Verilator on PATH. Cogni runs `verilator --lint-only -Wall`
across `*.sv` / `*.v` under `--rtl-root`, parses warnings into the
`rtl.*` measurement namespace, then sweeps the pack.

### Synth stage — netlist mode

```bash
PYTHONPATH=. python3 cogni_check.py \
    --stage synth \
    --netlist path/to/syn_netlist.v \
    --top-module my_top
```

Requires Yosys on PATH. Cogni emits a `stat` script, runs Yosys, and
parses the gate histogram.

### Synth stage — reports mode (recommended)

If you already ran Yosys and have a `stat.rpt`:

```bash
PYTHONPATH=. python3 cogni_check.py \
    --stage synth \
    --reports-dir path/to/yosys/reports
```

This is faster (no re-synthesis) and matches the way Cogni was
calibrated.

### Replay mode (no EDA tools needed)

Drop a `findings.json` next to your design:

```json
{
  "measurements": {
    "rtl.lint.latch.count": 2,
    "rtl.lint.blkseq.count": 2,
    "rtl.lint.width.count": 1
  }
}
```

Then:

```bash
PYTHONPATH=. python3 cogni_check.py --stage rtl \
    --findings my_findings.json
```

## 9. Authoring a scenario directory

```
my_chip/
    config.yaml         # stage, tool, optional rtl_root / reports_dir
    manifest.json       # facts + tags fed to the perceiver (e.g. pdk_sky130)
    findings.json       # OR a reports/ dir — replay or live
    rtl/                # optional, only if you want live verilator
```

Minimal `config.yaml`:

```yaml
stage: rtl
tool: verilator
pack_path: packs/rtl/rules.json
perceiver:
  manifest_path: manifest.json
oracle:
  findings_path: findings.json
```

Minimal `manifest.json` (drives discriminator tags + PDK gating):

```json
{
  "facts": {
    "design.intended_flop_based": true,
    "pdk.name": "sky130hd"
  },
  "tags": ["intended_flop_based", "pdk_sky130"]
}
```

PDK rules only fire when the `pdk_*` tag matches. Omit it and they
skip silently.

## 10. Quick reference

| Command | Purpose |
|---------|---------|
| `pytest tests/ -q` | run 92-test suite |
| `cogni_check.py <scn> --stage rtl` | sweep only |
| `cogni_check.py <scn> --stage rtl --propose-fixes --test-mode` | sweep + fixes (cheap) |
| `cogni_check.py <scn> --stage rtl --propose-fixes` | sweep + fixes (prod models) |
| `cogni_check.py --stage rtl --rtl-root <dir> --top-module <name>` | live RTL on your design |
| `cogni_check.py --stage synth --netlist <file>` | live netlist |
| `cogni_check.py --stage synth --reports-dir <dir>` | Yosys reports replay |

## 11. Where to look when things go wrong

- `REPORT.md` summary section — high-level outcome
- `REPORT.json` — full machine-readable record with every check
- `<report-dir>/llm_calls/<call-name>/` — exact prompt, schema, input,
  output for every LLM call (only present with `--propose-fixes`)
- `docs/RUNBOOK.md` — common failure modes and fixes
- `FIXES_LOG.md` — every non-trivial bug we've already hit
