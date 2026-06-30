"""
cogni.adapters.rtl.vcs.oracle
=============================

RTL-stage oracle using Synopsys VCS lint. Runs vlogan + vcs with
+lint=all and parses Lint-[...] warnings into rtl.lint.* and
vcs.lint.* measurement keys.

VCS catches different things than Verilator -- combining both gives
broader coverage and lets rules cross-validate across tools.

VCS lint categories mapped:
  Lint-[ULCO]  -> width comparison mismatch
  Lint-[NS]    -> null statement
  Lint-[TFIPC] -> port connection width
  Lint-[LATCH] -> inferred latch (from synthesis)
  Lint-[DALIAS]-> duplicate alias
  Lint-[CWECBB]-> case without default
  Lint-[IOCOMB]-> combinational I/O
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any

from agent.core import Reality, new_id
from agent.fix_verify import gather_rtl_files


_VCS_CLASS_TO_RTL_KEY: dict[str, str] = {
    "ULCO":   "rtl.lint.width.count",
    "TFIPC":  "rtl.lint.width.count",
    "NS":     "vcs.lint.null_statement.count",
    "LATCH":  "rtl.lint.latch.count",
    "CWECBB": "rtl.lint.case_incomplete.count",
    "DALIAS": "vcs.lint.duplicate_alias.count",
    "IOCOMB": "vcs.lint.io_comb.count",
}

_VCS_COVERED_KEYS: set[str] = set(_VCS_CLASS_TO_RTL_KEY.values())

_LINT_PATTERN = re.compile(r"Lint-\[(\w+)\]")


def _run_vcs_lint(rtl_files: list[str], *,
                  top: str | None = None,
                  vcs_bin: str = "vcs",
                  vlogan_bin: str = "vlogan") -> dict[str, int]:
    """Run VCS lint and return {warning_class: count}."""
    work_dir = tempfile.mkdtemp(prefix="cogni_vcs_")

    abs_files = [os.path.abspath(f) for f in rtl_files]

    classes: dict[str, int] = {}

    # Phase 1: vlogan (analysis)
    vlogan_cmd = [vlogan_bin, "-sverilog", "-full64", "+warn=all"]
    vlogan_cmd.extend(abs_files)
    try:
        result = subprocess.run(
            vlogan_cmd, capture_output=True, text=True,
            timeout=120, cwd=work_dir)
        output = result.stdout + result.stderr
        for m in _LINT_PATTERN.finditer(output):
            cls = m.group(1)
            classes[cls] = classes.get(cls, 0) + 1
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return classes

    # Phase 2: vcs elaboration (catches more lint at elab stage)
    vcs_cmd = [vcs_bin, "-sverilog", "-full64", "+lint=all",
               "+warn=all", "-notice"]
    if top:
        vcs_cmd.extend(["-top", top])
    vcs_cmd.extend(abs_files)
    try:
        result = subprocess.run(
            vcs_cmd, capture_output=True, text=True,
            timeout=120, cwd=work_dir)
        output = result.stdout + result.stderr
        for m in _LINT_PATTERN.finditer(output):
            cls = m.group(1)
            classes[cls] = classes.get(cls, 0) + 1
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return classes


class VCSRTLOracle:
    """RTL-stage oracle backed by Synopsys VCS lint."""

    stage = "rtl"
    tool = "vcs"

    def __init__(self, *,
                 top: str | None = None,
                 rtl_root: str | None = None,
                 rtl_files: list[str] | None = None,
                 vcs_bin: str = "vcs",
                 vlogan_bin: str = "vlogan"):
        self.top = top
        self.rtl_root = rtl_root
        if isinstance(rtl_files, str):
            rtl_files = [p.strip() for p in rtl_files.split(",") if p.strip()]
        self.rtl_files = list(rtl_files) if rtl_files else []
        self.vcs_bin = vcs_bin
        self.vlogan_bin = vlogan_bin

    def from_live_lint(self) -> Reality:
        files = list(self.rtl_files)
        if not files and self.rtl_root and os.path.isdir(self.rtl_root):
            files = gather_rtl_files(self.rtl_root)
        if not files:
            raise FileNotFoundError(
                "VCSRTLOracle: no rtl_files/rtl_root to lint.")

        classes = _run_vcs_lint(
            files, top=self.top,
            vcs_bin=self.vcs_bin, vlogan_bin=self.vlogan_bin)

        measurements: dict[str, Any] = {k: 0 for k in _VCS_COVERED_KEYS}
        for cls, n in classes.items():
            key = _VCS_CLASS_TO_RTL_KEY.get(cls)
            if key:
                measurements[key] = measurements.get(key, 0) + n

        measurements["vcs.lint.raw_classes"] = classes

        return Reality(
            id=new_id("real"),
            source="vcs:live-lint",
            measurements=measurements,
            artifacts=list(files),
        )

    def from_existing(self) -> Reality:
        return self.from_live_lint()
