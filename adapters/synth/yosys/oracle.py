"""
cogni.adapters.synth.yosys.oracle
===================
Reality-source for VLSI synthesis questions. Wraps Yosys + (optional) sv2v
and parses the resulting reports into a `Reality` object with measurements
the Verdict step can compare against predictions.

Two modes:
  - `from_existing(reports_dir, findings_path)` — reuse already-generated
    Yosys output. Fast. Used for re-runs where the design hasn't changed.
  - `run_synthesis(rtl_dir, top, work_dir)` — invoke sv2v + yosys fresh.
    Slow. Used when the design or constraints change.

The oracle is intentionally narrow. It answers questions of the form:
"after generic synthesis, what does Yosys report?" — totals, per-module
ranks, gate-type distribution. It does not run place-and-route. ORFS-style
PPA reality is a separate oracle.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
from typing import Any
from agent.core import Reality, new_id


# ---------------------------------------------------------------------------
# Stat-report parser
# ---------------------------------------------------------------------------
# Yosys's `stat.rpt` ends with a top-level summary block. We extract:
#   total_cells, total_wires, total_ff, gate_type_counts, hierarchy_top_module.
# The rest of the report (per-submodule blocks) is left for the findings
# JSON to carry — that's already done by cognipd's analyze pass.

_FF_GATE_PREFIXES = ("$_DFF", "$_DFFE", "$_SDFF", "$_LATCH")


def parse_stat_rpt(path: str) -> dict[str, Any]:
    with open(path) as f:
        text = f.read()

    # Top-level totals are in the LAST stat block (after === design hierarchy ===)
    last = text.rsplit("=== design hierarchy ===", 1)[-1]

    def grab(label: str) -> int | None:
        m = re.search(rf"Number of {label}:\s+(\d+)", last)
        return int(m.group(1)) if m else None

    totals = {
        "wires":      grab("wires"),
        "wire_bits":  grab("wire bits"),
        "ports":      grab("ports"),
        "cells":      grab("cells"),
    }

    # Gate type histogram from the last block
    gate_counts: dict[str, int] = {}
    for line in last.splitlines():
        m = re.match(r"\s+(\$_\w+_)\s+(\d+)\s*$", line)
        if m:
            gate_counts[m.group(1)] = int(m.group(2))

    ff_cells = sum(c for g, c in gate_counts.items()
                   if any(g.startswith(p) for p in _FF_GATE_PREFIXES))
    comb_cells = (totals["cells"] or 0) - ff_cells

    # Sort gates by count
    top_gates = sorted(gate_counts.items(), key=lambda kv: -kv[1])

    return {
        "total_cells":  totals["cells"],
        "total_wires":  totals["wires"],
        "total_ff":     ff_cells,
        "total_comb":   comb_cells,
        "gate_counts":  gate_counts,
        "top_gates":    top_gates[:10],
        "raw_totals":   totals,
    }


# ---------------------------------------------------------------------------
# Oracle entry points
# ---------------------------------------------------------------------------

class YosysOracle:
    """Returns a Reality object for synthesis-stage questions."""

    def __init__(self, reports_dir: str, findings_path: str | None = None,
                 source_label: str = "yosys+sv2v"):
        self.reports_dir = reports_dir
        self.findings_path = findings_path
        self.source_label = source_label

    def from_existing(self) -> Reality:
        stat_path = os.path.join(self.reports_dir, "stat.rpt")
        if not os.path.exists(stat_path):
            raise FileNotFoundError(f"Missing stat.rpt at {stat_path}")
        stats = parse_stat_rpt(stat_path)

        findings = {}
        if self.findings_path and os.path.exists(self.findings_path):
            with open(self.findings_path) as f:
                findings = json.load(f)

        # Build merged measurements dict — flat keys for easy comparison
        total_cells = stats["total_cells"] or 0
        total_ff    = stats["total_ff"] or 0
        gate_counts = stats["gate_counts"] or {}
        # Inverter / buffer pressure: any gate whose name contains NOT or BUF
        # (Yosys generic library) — ANDNOT/ORNOT count as inv-pressure too.
        invbuf_cells = sum(c for g, c in gate_counts.items()
                           if ("NOT" in g) or ("BUF" in g))
        latch_cells  = sum(c for g, c in gate_counts.items() if "LATCH" in g)
        m = {
            "synth.total_cells":   stats["total_cells"],
            "synth.total_wires":   stats["total_wires"],
            "synth.total_ff":      stats["total_ff"],
            "synth.total_comb":    stats["total_comb"],
            "synth.top_gates":     stats["top_gates"],
            "synth.gate_counts":   stats["gate_counts"],
        }
        # v1-pack derived shares (only emit when total > 0 to avoid div/0).
        if total_cells > 0:
            m["synth.ff_share_pct"]     = round(100.0 * total_ff / total_cells, 2)
            m["synth.invbuf_share_pct"] = round(100.0 * invbuf_cells / total_cells, 2)
        if total_ff > 0:
            m["synth.icg_per_1kff"] = round(1000.0 * latch_cells / total_ff, 4)
        # Latch warnings: count of LATCH-class cells in generic netlist is the
        # closest free signal. Real flow would parse yosys.log Warning lines.
        m["synth.warnings.latch"] = int(latch_cells)

        # Pull module ranking out of findings if present
        ranking = findings.get("module_combinational_ranking_top10") or []
        if ranking:
            m["synth.top_module_by_cells"] = ranking[0].get("module")
            m["synth.top_module_cell_count"] = ranking[0].get("total_cells")
            m["synth.module_ranking"] = [
                {"module": r["module"], "total_cells": r["total_cells"],
                 "comb_cells": r.get("comb_cells"), "ff_cells": r.get("ff_cells")}
                for r in ranking
            ]
        # Critical-fanout / longest-path findings if present
        if "longest_topo_path_in_seq_logic" in findings:
            m["synth.longest_topo_seq_path"] = findings["longest_topo_path_in_seq_logic"]
        if "high_fanout_signals_top10" in findings:
            m["synth.high_fanout_top10"] = findings["high_fanout_signals_top10"]

        artifacts = [stat_path]
        if self.findings_path:
            artifacts.append(self.findings_path)

        return Reality(
            id=new_id("real"),
            source=self.source_label,
            measurements=m,
            artifacts=artifacts,
        )

    # -- fresh synthesis path (kept simple; reuse cognipd's tcl) -------------
    def run_synthesis(self, rtl_dir: str, top: str, work_dir: str,
                      tcl_template: str = "") -> Reality:
        os.makedirs(work_dir, exist_ok=True)
        # Defer to cognipd's existing yosys_synth.tcl if present, else fail.
        # The point of this class is parsing, not orchestrating Yosys.
        if not tcl_template:
            tcl_template = "../cognipd/runs/yosys_synth.tcl"
        if not os.path.exists(tcl_template):
            raise FileNotFoundError(f"yosys tcl not found: {tcl_template}")
        # Run it
        log = os.path.join(work_dir, "yosys.log")
        with open(log, "w") as f:
            r = subprocess.run(["yosys", "-q", "-c", tcl_template],
                               cwd=work_dir, stdout=f, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            raise RuntimeError(f"yosys failed; see {log}")
        # Reports path is whatever the tcl wrote; assume same as reports_dir
        return self.from_existing()
