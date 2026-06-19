#!/usr/bin/env python3
"""
N-run ensemble harness for determinism / A-B prompt testing.

Runs the full `run_real.py run-all-api` pipeline N times back-to-back
(or in parallel when --parallel >1), each in its own session_dir, with a
distinct seed per run. Then aggregates per-question verdicts into
ensemble_metrics.json.

Why a separate harness instead of an inline `--n-runs` flag?
- Subprocess isolation guarantees no leaked module/global state between
  runs (matters for the test-mode swap and for transport client caches).
- Reuses the existing CLI surface unchanged; if run-all-api works, this
  works.
- Per-run logs land in their own session_dir, so a single bad run is
  trivial to inspect without touching the others.

Usage:
    PYTHONPATH=. python3 run_ensemble.py \\
        scenarios/rtl_demo \\
        --n-runs 3 \\
        --test-mode \\
        --concurrency 10

    # A-B testing: tag each ensemble with a label, then diff metrics.
    PYTHONPATH=. python3 run_ensemble.py scenarios/rtl_demo \\
        --n-runs 5 --tag baseline --test-mode

Output layout:
    runs/ensembles/<UTC-ts>_<tag>/
        run_001/                <- standard session_dir, moved from runs/real/
            summary.json
            metrics.json
            ...
        run_002/
        ...
        ensemble_metrics.json   <- aggregate
        ensemble.log            <- merged stdout/stderr
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SESSION_ROOT = ROOT / "runs" / "real"
# Ensembles live in a sibling dir so the ensemble dir name itself never
# collides with run-all-api's "latest session_dir under SESSION_ROOT"
# auto-discovery (which sorts alphabetically and would otherwise pick up
# our ensemble_<ts> dir as if it were a session).
ENSEMBLE_ROOT = ROOT / "runs" / "ensembles"


# ---------------------------------------------------------------------------
# Per-run execution
# ---------------------------------------------------------------------------

def _run_one(scenario_dirs: list[str],
             run_idx: int,
             seed: int,
             concurrency: int,
             test_mode: bool,
             ensemble_dir: Path,
             log_fp) -> dict:
    """Invoke run_real.py run-all-api once, then move its session_dir
    under ensemble_dir/run_<idx>/. Returns a dict with run metadata."""
    run_label = f"run_{run_idx:03d}"
    run_dir = ensemble_dir / run_label
    log_fp.write(f"\n{'=' * 70}\n[ensemble] {run_label}  seed={seed}\n{'=' * 70}\n")
    log_fp.flush()

    # Snapshot existing session dirs so we can find the new one after the
    # subprocess returns. (run-all-api auto-discovers latest under
    # SESSION_ROOT — we mimic that here.)
    before = set(os.listdir(SESSION_ROOT)) if SESSION_ROOT.exists() else set()

    cmd = [sys.executable, "run_real.py", "run-all-api",
           *scenario_dirs,
           "--concurrency", str(concurrency)]
    if test_mode:
        cmd.append("--test-mode")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["COGNI_SEED"] = str(seed)

    t0 = time.time()
    proc = subprocess.run(
        cmd, cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    elapsed = time.time() - t0

    log_fp.write(proc.stdout or "")
    log_fp.write(f"\n[ensemble] {run_label} exit={proc.returncode}  wall={elapsed:.1f}s\n")
    log_fp.flush()

    after = set(os.listdir(SESSION_ROOT)) if SESSION_ROOT.exists() else set()
    new_sessions = sorted(after - before)
    if not new_sessions:
        return {"run": run_label, "ok": False, "seed": seed,
                "elapsed_s": elapsed, "exit": proc.returncode,
                "session_dir": None,
                "error": "no new session_dir produced"}

    src = SESSION_ROOT / new_sessions[-1]
    # Move (not copy) so we don't double up disk usage.
    shutil.move(str(src), str(run_dir))

    summary_path = run_dir / "summary.json"
    metrics_path = run_dir / "metrics.json"
    rec = {"run": run_label, "ok": proc.returncode == 0,
           "seed": seed, "elapsed_s": elapsed, "exit": proc.returncode,
           "session_dir": str(run_dir),
           "summary_path": str(summary_path) if summary_path.exists() else None,
           "metrics_path": str(metrics_path) if metrics_path.exists() else None}
    return rec


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _aggregate(run_records: list[dict]) -> dict:
    """Compute verdict variance per (scenario, qid) across runs, plus
    cost rollup."""
    # Per-question: list of verdicts across runs.
    verdicts: dict[tuple[str, str], list[dict]] = defaultdict(list)
    # Cost / behavior totals.
    total_cost_usd = 0.0
    total_in_tok = 0
    total_out_tok = 0
    total_calls = 0
    total_predictions = 0
    total_with_struct = 0
    n_silent_failures = 0

    per_run_brief = []

    for r in run_records:
        brief = {"run": r["run"], "ok": r["ok"], "seed": r["seed"],
                 "elapsed_s": round(r["elapsed_s"], 1),
                 "verdicts": {}, "totals": {},
                 "cost_usd": 0.0}
        if r.get("summary_path") and os.path.exists(r["summary_path"]):
            with open(r["summary_path"]) as f:
                summ = json.load(f)
            brief["totals"] = summ.get("totals", {})
            for scen_name, scen in (summ.get("scenarios") or {}).items():
                for q in scen.get("questions", []):
                    qid = q.get("id")
                    kind = q.get("verdict") or q.get("outcome") or "no_output"
                    verdicts[(scen_name, qid)].append({
                        "run": r["run"],
                        "verdict": kind,
                        "confidence": q.get("confidence"),
                        "claim": (q.get("claim") or "")[:200],
                    })
                    brief["verdicts"][f"{scen_name}.{qid}"] = kind
            metrics = summ.get("metrics") or {}
            brief["cost_usd"] = round(metrics.get("cost_usd", 0.0), 4)
            total_cost_usd += metrics.get("cost_usd", 0.0)
            total_in_tok += metrics.get("input_tokens", 0)
            total_out_tok += metrics.get("output_tokens", 0)
            total_calls += metrics.get("n_calls", 0)
            beh = summ.get("behavior") or {}
            total_predictions += beh.get("n_predictions", 0)
            total_with_struct += beh.get("n_with_structured_claim", 0)
            if summ.get("errors"):
                n_silent_failures += len(summ["errors"])
        per_run_brief.append(brief)

    # Per-question variance: agreement = max count / n_runs.
    n_runs = len(run_records)
    per_question = []
    n_unanimous = 0
    n_split = 0
    for (scen, qid), entries in sorted(verdicts.items()):
        kinds = [e["verdict"] for e in entries]
        ctr = Counter(kinds)
        most_kind, most_n = ctr.most_common(1)[0]
        agreement = most_n / len(entries) if entries else 0.0
        is_unanimous = len(ctr) == 1
        if is_unanimous:
            n_unanimous += 1
        else:
            n_split += 1
        per_question.append({
            "scenario": scen,
            "qid": qid,
            "n_observed": len(entries),
            "verdicts": kinds,
            "agreement_pct": round(agreement * 100, 1),
            "modal_verdict": most_kind,
            "unanimous": is_unanimous,
            "distribution": dict(ctr),
        })

    overall_agreement = (
        n_unanimous / (n_unanimous + n_split) if (n_unanimous + n_split) else 0.0
    )

    return {
        "n_runs": n_runs,
        "n_runs_ok": sum(1 for r in run_records if r["ok"]),
        "ensemble": {
            "n_questions_observed": len(per_question),
            "n_unanimous": n_unanimous,
            "n_split": n_split,
            "agreement_pct": round(overall_agreement * 100, 1),
        },
        "cost": {
            "total_usd": round(total_cost_usd, 4),
            "per_run_avg_usd": round(total_cost_usd / n_runs, 4) if n_runs else 0.0,
            "input_tokens": total_in_tok,
            "output_tokens": total_out_tok,
            "n_calls": total_calls,
        },
        "behavior": {
            "n_predictions": total_predictions,
            "n_with_structured_claim": total_with_struct,
            "structured_claim_coverage_pct": round(
                100.0 * total_with_struct / total_predictions, 1
            ) if total_predictions else 0.0,
            "n_silent_failures": n_silent_failures,
        },
        "runs": per_run_brief,
        "per_question": per_question,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the cogni pipeline N times for determinism / A-B testing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("scenarios", nargs="+",
                    help="One or more scenario dirs (e.g. scenarios/rtl_demo)")
    ap.add_argument("--n-runs", type=int, default=3,
                    help="Number of independent runs (default 3)")
    ap.add_argument("--seed-base", type=int, default=42,
                    help="Seeds used are seed-base + run_idx (default 42)")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="Per-stage concurrency for run-all-api (default 8)")
    ap.add_argument("--test-mode", action="store_true",
                    help="Pass --test-mode to each underlying run (Sonnet+gpt-5-mini)")
    ap.add_argument("--tag", default="",
                    help="Optional tag suffix for the ensemble dir name")
    args = ap.parse_args()

    if args.n_runs < 1:
        print("--n-runs must be >= 1", file=sys.stderr)
        return 2

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{args.tag}" if args.tag else ""
    ENSEMBLE_ROOT.mkdir(parents=True, exist_ok=True)
    ensemble_dir = ENSEMBLE_ROOT / f"{ts}{suffix}"
    ensemble_dir.mkdir(parents=True, exist_ok=True)
    log_path = ensemble_dir / "ensemble.log"

    print(f"[ensemble] dir = {ensemble_dir}")
    print(f"[ensemble] n_runs={args.n_runs}  seed_base={args.seed_base}  "
          f"test_mode={args.test_mode}  scenarios={args.scenarios}")

    overall_t0 = time.time()
    run_records = []
    with open(log_path, "w") as log_fp:
        log_fp.write(f"# ensemble run started {ts} UTC\n")
        log_fp.write(f"# scenarios = {args.scenarios}\n")
        log_fp.write(f"# n_runs = {args.n_runs}, seed_base = {args.seed_base}\n")
        log_fp.write(f"# test_mode = {args.test_mode}\n")
        for i in range(args.n_runs):
            seed = args.seed_base + i
            print(f"[ensemble] starting run_{i+1:03d}/{args.n_runs}  (seed={seed})",
                  flush=True)
            rec = _run_one(
                scenario_dirs=args.scenarios,
                run_idx=i + 1,
                seed=seed,
                concurrency=args.concurrency,
                test_mode=args.test_mode,
                ensemble_dir=ensemble_dir,
                log_fp=log_fp,
            )
            run_records.append(rec)
            status = "OK" if rec["ok"] else "FAIL"
            print(f"[ensemble] run_{i+1:03d} {status}  wall={rec['elapsed_s']:.1f}s",
                  flush=True)

    aggregate = _aggregate(run_records)
    aggregate["ensemble_dir"] = str(ensemble_dir)
    aggregate["scenarios"] = args.scenarios
    aggregate["test_mode"] = args.test_mode
    aggregate["seed_base"] = args.seed_base
    aggregate["wall_s_total"] = round(time.time() - overall_t0, 1)

    out_path = ensemble_dir / "ensemble_metrics.json"
    with open(out_path, "w") as f:
        json.dump(aggregate, f, indent=2, default=str)

    # Print compact human summary.
    print()
    print("=" * 70)
    print(f"[ensemble] complete — {aggregate['n_runs_ok']}/{aggregate['n_runs']} runs OK  "
          f"wall={aggregate['wall_s_total']:.1f}s")
    print("=" * 70)
    e = aggregate["ensemble"]; c = aggregate["cost"]; b = aggregate["behavior"]
    print(f"  agreement       : {e['agreement_pct']}%  "
          f"({e['n_unanimous']} unanimous / {e['n_split']} split / "
          f"{e['n_questions_observed']} questions)")
    print(f"  cost            : ${c['total_usd']:.4f} total  "
          f"(${c['per_run_avg_usd']:.4f}/run, "
          f"{c['input_tokens']:,} in / {c['output_tokens']:,} out, "
          f"{c['n_calls']} calls)")
    print(f"  structured_claim: {b['n_with_structured_claim']}/{b['n_predictions']} "
          f"({b['structured_claim_coverage_pct']}%)")
    if b["n_silent_failures"]:
        print(f"  silent_failures : {b['n_silent_failures']}  (see per-run metrics.json)")
    print(f"  ensemble_metrics: {out_path}")

    if e["n_split"]:
        print()
        print("  Split questions (non-unanimous):")
        for q in aggregate["per_question"]:
            if not q["unanimous"]:
                dist = ", ".join(f"{k}:{v}" for k, v in q["distribution"].items())
                print(f"    {q['scenario']}.{q['qid']:<24} agreement={q['agreement_pct']}%  "
                      f"[{dist}]")

    return 0 if aggregate["n_runs_ok"] == aggregate["n_runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
