"""
cogni.adapters.rtl.verilator.runner
===================================
Run `verilator --lint-only` over an RTL tree and convert warnings into
the `rtl.*` measurement_key namespace the pack predicts on.

Falls back to a precomputed findings.json when verilator is not on PATH
(sandbox / replay). The returned object is shape-compatible with what
sweep / fixer expect: an object with a `.measurements` dict.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LintFinding:
    file: str
    line: int
    code: str          # e.g. LATCH, BLKSEQ, WIDTH, CASEINCOMPLETE
    message: str


@dataclass
class VerilatorReality:
    measurements: dict[str, Any] = field(default_factory=dict)
    findings: list[LintFinding] = field(default_factory=list)
    raw_log: str = ""
    source: str = ""   # "verilator" | "findings_json"


# Verilator code → cogni rtl.lint.<bucket>.count mapping. The pack uses
# canonical buckets — keep this table small and explicit.
_VLINT_BUCKETS = {
    "LATCH":           "rtl.lint.latch.count",
    "CASEINCOMPLETE":  "rtl.lint.case_incomplete.count",
    "CASEOVERLAP":     "rtl.lint.case_overlap.count",
    "BLKSEQ":          "rtl.lint.blkseq.count",
    "BLKLOOPINIT":     "rtl.lint.blkseq.count",   # related family
    "WIDTH":           "rtl.lint.width.count",
    "WIDTHCONCAT":     "rtl.lint.width.count",
    "UNUSED":          "rtl.lint.unused.count",
    "UNDRIVEN":        "rtl.lint.undriven.count",
    "MULTIDRIVEN":     "rtl.lint.multidriven.count",
    "ALWCOMBORDER":    "rtl.lint.alwcomborder.count",
    "COMBDLY":         "rtl.lint.combdly.count",
    "STMTDLY":         "rtl.lint.stmtdly.count",
    "REALCVT":         "rtl.lint.realcvt.count",
}

_VLINT_RE = re.compile(
    r"^%(?:Warning|Error)-(?P<code>[A-Z0-9_]+):\s+"
    r"(?P<file>[^:]+):(?P<line>\d+):(?:\d+:)?\s*(?P<msg>.*)$"
)


def _parse_verilator_log(log: str) -> list[LintFinding]:
    out = []
    for line in log.splitlines():
        m = _VLINT_RE.match(line)
        if not m:
            continue
        out.append(LintFinding(
            file=m.group("file"),
            line=int(m.group("line")),
            code=m.group("code").upper(),
            message=m.group("msg").strip(),
        ))
    return out


def _aggregate(findings: list[LintFinding]) -> dict[str, Any]:
    """Roll lint findings up into the rtl.lint.* namespace."""
    counts: dict[str, int] = {}
    for f in findings:
        bucket = _VLINT_BUCKETS.get(f.code)
        if bucket is None:
            bucket = f"rtl.lint.{f.code.lower()}.count"
        counts[bucket] = counts.get(bucket, 0) + 1
    counts["rtl.lint.total.count"] = sum(counts.values())
    return counts


def from_findings_json(path: str) -> VerilatorReality:
    """Replay path: read precomputed measurements + (optional) findings list."""
    with open(path) as f:
        data = json.load(f)
    meas = data.get("measurements") or {k: v for k, v in data.items()
                                         if isinstance(k, str) and k.startswith("rtl.")}
    fl = []
    for f in data.get("findings", []):
        fl.append(LintFinding(
            file=f.get("file", ""), line=int(f.get("line", 0) or 0),
            code=str(f.get("code", "")).upper(), message=f.get("message", ""),
        ))
    return VerilatorReality(measurements=dict(meas), findings=fl,
                            source="findings_json")


def run_verilator(rtl_root: str,
                  *,
                  top_module: str | None = None,
                  extra_args: list[str] | None = None,
                  timeout_s: int = 120) -> VerilatorReality:
    """Run verilator --lint-only across rtl_root. Caller should pass the
    discovered top file via top_module if known.

    Raises FileNotFoundError if verilator is not on PATH — caller is
    expected to fall back to from_findings_json in that case.
    """
    if shutil.which("verilator") is None:
        raise FileNotFoundError("verilator not on PATH")

    sv_files = []
    for dp, _, fs in os.walk(rtl_root):
        for fn in fs:
            if fn.endswith((".sv", ".v")):
                sv_files.append(os.path.join(dp, fn))
    if not sv_files:
        raise FileNotFoundError(f"no .sv/.v under {rtl_root}")

    cmd = ["verilator", "--lint-only", "-Wall", "-Wno-fatal", "-sv"]
    if top_module:
        cmd += ["--top-module", top_module]
    if extra_args:
        cmd += list(extra_args)
    cmd += sv_files

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    log = (proc.stdout or "") + "\n" + (proc.stderr or "")
    findings = _parse_verilator_log(log)
    measurements = _aggregate(findings)
    return VerilatorReality(measurements=measurements, findings=findings,
                            raw_log=log, source="verilator")


def observe(rtl_root: str | None = None,
            *,
            findings_path: str | None = None,
            top_module: str | None = None) -> VerilatorReality:
    """Convenience: prefer live tool, fall back to findings.json."""
    if findings_path and os.path.exists(findings_path):
        # Always honor explicit replay if available — keeps runs deterministic.
        return from_findings_json(findings_path)
    if rtl_root and os.path.isdir(rtl_root):
        try:
            return run_verilator(rtl_root, top_module=top_module)
        except FileNotFoundError:
            pass
    raise FileNotFoundError(
        "neither verilator nor findings.json available "
        f"(rtl_root={rtl_root!r}, findings={findings_path!r})"
    )
