"""Oracle CI harness: diff Cogni's pure-Python inference against Verilator.

Verilator is a real elaborator (unrolls generates/loops, resolves params and
widths), so its post-elaboration resource counts are ground truth. This harness
runs both paths across a design suite and reports where Cogni's pure-Python
inference diverges.

CI semantics
------------
Not every divergence is a Cogni bug -- some are defensible modeling choices
(e.g. `mem` counted as a memory array, not flip-flops; a `case` decode counted
as a mux, not N comparators). Those live in a baseline. The harness FAILS only
on a *regression*: Cogni's undercount (or overcount) getting worse than the
recorded baseline. Improvements pass and prompt a baseline refresh.

If verilator is not installed the harness SKIPS (exit 0) -- it never blocks CI
in environments without the tool.

Usage
-----
    python -m agent.oracle_check                 # check against baseline
    python -m agent.oracle_check --update        # refresh the baseline
    python -m agent.oracle_check --verbose       # full per-metric table
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(__file__)
_ROOT = os.path.dirname(_HERE)
_BASELINE = os.path.join(_HERE, "oracle_baseline.json")

# Designs that elaborate cleanly under Verilator (self-contained, single top).
SUITE = [
    "tests/oracle_fixtures/pwm_generator.sv",
    "tests/oracle_fixtures/sync_fifo.sv",
    "tests/oracle_fixtures/dual_port_ram.sv",
    "scenarios/buggy_demo/rtl/spi_master.sv",
    "scenarios/buggy_demo/rtl/i2c_master.sv",
    "scenarios/buggy_demo/rtl/alu.sv",
    "scenarios/buggy_demo/rtl/async_fifo.sv",
]

METRICS = ("ff_bits", "comparators", "adders", "multipliers")


def _cogni_counts(path: str) -> dict:
    from agent.rtl_analyzer import analyze_design
    r = analyze_design([path])           # pure-Python path (no overlay)
    syn = r.measurements.get("cogni.synth", {})
    ops = syn.get("operators", {})
    return {
        "ff_bits": sum(syn.get("ff_signals", {}).values()),
        "comparators": ops.get("comparators", 0),
        "adders": ops.get("adders", 0),
        "multipliers": ops.get("multipliers", 0),
    }


def _verilator_counts(path: str) -> dict | None:
    from agent.verilator_elab import elaborate_with_verilator
    ve = elaborate_with_verilator([path])
    if not ve or not ve.ok:
        return None
    return {"ff_bits": ve.ff_bits, "comparators": ve.comparators,
            "adders": ve.adders, "multipliers": ve.multipliers}


def _abspath(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(_ROOT, p)


def collect() -> dict:
    """Run both paths across the suite. Returns {design: {metric: (cogni, verilator)}}."""
    out = {}
    for rel in SUITE:
        path = _abspath(rel)
        if not os.path.isfile(path):
            continue
        v = _verilator_counts(path)
        if v is None:            # did not elaborate -- skip this design
            continue
        c = _cogni_counts(path)
        name = os.path.splitext(os.path.basename(rel))[0]
        out[name] = {m: (c[m], v[m]) for m in METRICS}
    return out


def _divergence(cogni: int, veri: int) -> tuple[int, int]:
    """(undercount, overcount) of Cogni relative to Verilator ground truth."""
    return max(0, veri - cogni), max(0, cogni - veri)


def load_baseline() -> dict:
    if os.path.isfile(_BASELINE):
        with open(_BASELINE) as f:
            return json.load(f)
    return {}


def build_baseline(data: dict) -> dict:
    bl = {}
    for name, metrics in data.items():
        bl[name] = {}
        for m, (c, v) in metrics.items():
            under, over = _divergence(c, v)
            bl[name][m] = [under, over]
    return bl


def check(verbose: bool = False) -> int:
    if not _verilator_present():
        print("verilator not installed -- oracle check SKIPPED (not a failure).")
        return 0

    data = collect()
    if not data:
        print("No designs elaborated -- oracle check SKIPPED.")
        return 0
    baseline = load_baseline()
    regressions = []
    improvements = []

    for name, metrics in sorted(data.items()):
        for m, (c, v) in metrics.items():
            under, over = _divergence(c, v)
            base = baseline.get(name, {}).get(m, [0, 0])
            b_under, b_over = base[0], base[1]
            if under > b_under or over > b_over:
                regressions.append((name, m, c, v, (b_under, b_over), (under, over)))
            elif under < b_under or over < b_over:
                improvements.append((name, m, c, v))
            if verbose:
                tag = "exact" if under == over == 0 else (
                    "gap+%d" % under if under else "over+%d" % over)
                print("  %-18s %-12s cogni=%-3d verilator=%-3d  [%s]"
                      % (name, m, c, v, tag))

    print("\n=== Oracle check: Cogni vs Verilator ground truth ===")
    print("designs: %d   metrics: %d" % (len(data), len(METRICS)))
    if improvements:
        print("\nIMPROVED since baseline (%d) -- run --update to record:" % len(improvements))
        for name, m, c, v in improvements:
            print("  %-18s %-12s cogni=%d verilator=%d" % (name, m, c, v))
    if regressions:
        print("\nREGRESSIONS (%d) -- Cogni drifted from ground truth:" % len(regressions))
        for name, m, c, v, base, now in regressions:
            print("  %-18s %-12s cogni=%d verilator=%d  baseline(under,over)=%s now=%s"
                  % (name, m, c, v, base, now))
        print("\nFAIL")
        return 1
    print("\nPASS -- no divergence beyond the accepted baseline.")
    return 0


def _verilator_present() -> bool:
    from agent.verilator_elab import verilator_available
    return verilator_available()


def main(argv: list[str]) -> int:
    if "--update" in argv:
        if not _verilator_present():
            print("verilator not installed -- cannot build baseline.")
            return 1
        data = collect()
        bl = build_baseline(data)
        with open(_BASELINE, "w") as f:
            json.dump(bl, f, indent=2, sort_keys=True)
        print("Baseline written: %d designs -> %s" % (len(bl), _BASELINE))
        return 0
    return check(verbose="--verbose" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
