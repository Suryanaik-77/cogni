"""
cogni.adapters.synth.yosys.runner
=================================
Thin wrapper around the existing YosysOracle so the new sweep CLI can
treat synth and rtl uniformly.

Three input modes (in priority order):
  1. findings_path  — a precomputed JSON with `synth.*` measurements.
                      Fastest, deterministic, sandbox-friendly.
  2. reports_dir    — directory containing Yosys's stat.rpt (and optional
                      synth_findings.json). Re-uses YosysOracle.from_existing().
  3. netlist_path   — single .v gate-level netlist. We synthesize a
                      Yosys script on the fly to emit `stat`, then parse.

Falls through to FileNotFoundError if none of the above resolve.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from .oracle import YosysOracle, parse_stat_rpt


@dataclass
class YosysReality:
    measurements: dict[str, Any] = field(default_factory=dict)
    raw_log: str = ""
    source: str = ""   # "findings_json" | "reports_dir" | "yosys"
    artifacts: list[str] = field(default_factory=list)


def _from_oracle_reality(reality) -> YosysReality:
    return YosysReality(
        measurements=dict(reality.measurements),
        source="reports_dir",
        artifacts=list(getattr(reality, "artifacts", []) or []),
    )


def from_findings_json(findings_path: str) -> YosysReality:
    with open(findings_path) as f:
        data = json.load(f)
    meas = data.get("measurements") or {k: v for k, v in data.items()
                                         if isinstance(k, str) and k.startswith("synth.")}
    return YosysReality(measurements=dict(meas), source="findings_json",
                        artifacts=[findings_path])


def from_reports_dir(reports_dir: str,
                     findings_path: str | None = None) -> YosysReality:
    oracle = YosysOracle(reports_dir=reports_dir, findings_path=findings_path)
    return _from_oracle_reality(oracle.from_existing())


def from_netlist(netlist_path: str,
                 *,
                 top_module: str | None = None,
                 timeout_s: int = 300) -> YosysReality:
    """Run yosys on a flat netlist to emit stat.rpt then parse it."""
    if shutil.which("yosys") is None:
        raise FileNotFoundError("yosys not on PATH")
    if not os.path.exists(netlist_path):
        raise FileNotFoundError(netlist_path)

    work = tempfile.mkdtemp(prefix="cogni_yosys_")
    stat_path = os.path.join(work, "stat.rpt")
    log_path = os.path.join(work, "yosys.log")

    top_arg = f"-top {top_module}" if top_module else ""
    script = (
        f"read_verilog -sv {netlist_path}\n"
        f"hierarchy -auto-top {top_arg}\n"
        f"proc; opt;\n"
        f"tee -o {stat_path} stat\n"
    )
    script_path = os.path.join(work, "run.ys")
    with open(script_path, "w") as f:
        f.write(script)

    with open(log_path, "w") as lf:
        proc = subprocess.run(
            ["yosys", "-q", "-s", script_path],
            stdout=lf, stderr=subprocess.STDOUT, timeout=timeout_s,
        )
    if proc.returncode != 0 or not os.path.exists(stat_path):
        raise RuntimeError(f"yosys failed; see {log_path}")

    stats = parse_stat_rpt(stat_path)
    total_cells = stats["total_cells"] or 0
    total_ff    = stats["total_ff"] or 0
    gate_counts = stats["gate_counts"] or {}
    invbuf_cells = sum(c for g, c in gate_counts.items()
                       if ("NOT" in g) or ("BUF" in g))
    latch_cells  = sum(c for g, c in gate_counts.items() if "LATCH" in g)
    m = {
        "synth.total_cells":  stats["total_cells"],
        "synth.total_wires":  stats["total_wires"],
        "synth.total_ff":     stats["total_ff"],
        "synth.total_comb":   stats["total_comb"],
        "synth.top_gates":    stats["top_gates"],
        "synth.gate_counts":  stats["gate_counts"],
    }
    if total_cells > 0:
        m["synth.ff_share_pct"]     = round(100.0 * total_ff / total_cells, 2)
        m["synth.invbuf_share_pct"] = round(100.0 * invbuf_cells / total_cells, 2)
    if total_ff > 0:
        m["synth.icg_per_1kff"] = round(1000.0 * latch_cells / total_ff, 4)
    m["synth.warnings.latch"] = int(latch_cells)

    return YosysReality(measurements=m, source="yosys",
                        artifacts=[stat_path, log_path])


def _gather_rtl(rtl_dir: str) -> list[str]:
    out = []
    for dp, _d, fs in os.walk(rtl_dir):
        for f in sorted(fs):
            if f.endswith((".sv", ".v")):
                out.append(os.path.join(dp, f))
    return sorted(out)


def from_rtl(rtl_dir: str, *, top: str | None = None,
             timeout_s: int = 300) -> YosysReality:
    """Actually SYNTHESIZE the RTL with Yosys (RTL -> generic gate netlist) and
    measure the result. This is the real gate-level step: it proves the RTL
    synthesizes AND surfaces hazards that only appear in the netlist —
    inferred latch CELLS ($_DLATCH_*) and multidriven nets — which RTL lint
    can miss.

    Raises FileNotFoundError if yosys isn't installed or there's no RTL, and
    RuntimeError if synthesis fails (e.g. unsupported SV) — the caller turns
    those into an honest 'netlist UNVERIFIED', never a false GO.
    """
    if shutil.which("yosys") is None:
        raise FileNotFoundError("yosys not on PATH")
    files = _gather_rtl(rtl_dir)
    if not files:
        raise FileNotFoundError(f"no .sv/.v under {rtl_dir}")

    work = tempfile.mkdtemp(prefix="cogni_synth_")
    stat_path = os.path.join(work, "stat.rpt")
    log_path = os.path.join(work, "yosys.log")
    synth_cmd = f"synth -top {top}" if top else "synth -auto-top"
    script = (
        "read_verilog -sv " + " ".join(files) + "\n"
        + synth_cmd + "\n"
        + f"tee -o {stat_path} stat\n"
    )
    script_path = os.path.join(work, "run.ys")
    with open(script_path, "w") as f:
        f.write(script)
    with open(log_path, "w") as lf:
        proc = subprocess.run(["yosys", "-q", "-s", script_path],
                              stdout=lf, stderr=subprocess.STDOUT, timeout=timeout_s)
    log = open(log_path, encoding="utf-8", errors="replace").read()
    if proc.returncode != 0 or not os.path.exists(stat_path):
        tail = "\n".join(log.splitlines()[-12:])
        raise RuntimeError(f"yosys synthesis failed (rc={proc.returncode}).\n{tail}")

    stats = parse_stat_rpt(stat_path)
    total_cells = stats["total_cells"] or 0
    total_ff = stats["total_ff"] or 0
    gate_counts = stats["gate_counts"] or {}
    latch_cells = sum(c for g, c in gate_counts.items() if "LATCH" in g.upper())
    m: dict[str, Any] = {
        "synth.total_cells": stats["total_cells"],
        "synth.total_ff": stats["total_ff"],
        "synth.total_comb": stats["total_comb"],
        "synth.gate_counts": gate_counts,
        # Hazards that only show up in the NETLIST:
        "synth.warnings.latch": int(latch_cells),
        "synth.warnings.multidriven":
            len(re.findall(r"multiple drivers|multidriven", log, re.I)),
    }
    if total_cells and total_ff:
        m["synth.ff_share_pct"] = round(100.0 * total_ff / total_cells, 2)
    return YosysReality(measurements=m, source="yosys-rtl",
                        raw_log=log, artifacts=[stat_path, log_path])


def observe(*,
            findings_path: str | None = None,
            reports_dir: str | None = None,
            netlist_path: str | None = None,
            top_module: str | None = None) -> YosysReality:
    """Pick the first input that resolves. Priority:
      reports_dir (with optional findings_path enrichment)
        > findings_path alone (must contain synth.* keys)
        > netlist_path (live yosys run).

    reports_dir wins because the oracle does the canonical aggregation
    (ff_share_pct, invbuf_share_pct, etc.) from stat.rpt; findings.json on
    its own only carries pre-flattened keys when produced by an upstream
    pipeline.
    """
    if reports_dir and os.path.isdir(reports_dir):
        return from_reports_dir(reports_dir, findings_path=findings_path)
    if findings_path and os.path.exists(findings_path):
        return from_findings_json(findings_path)
    if netlist_path and os.path.exists(netlist_path):
        return from_netlist(netlist_path, top_module=top_module)
    raise FileNotFoundError(
        "no synth input resolved "
        f"(findings={findings_path!r}, reports={reports_dir!r}, netlist={netlist_path!r})"
    )
