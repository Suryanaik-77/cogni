"""
cogni.adapters.rtl.verilator.oracle
===================================

RTL-stage oracle. Runs Verilator in lint/sim mode to produce
deterministic ground truth for RTL-stage questions — lint warnings,
CDC issues, FSM reachability, sim-based functional checks.

Status: SKELETON. Pairs with perceiver.py. See that file's docstring
for the motivation and phasing plan.

What needs to land here:

  1. Lint pass:
        verilator --lint-only -Wall -Wpedantic <top> \
            | parse into structured warnings keyed by rule-id

     Populate `rtl.lint.<rule_id>.count`, `rtl.lint.<rule_id>.locations`.

  2. CDC check:
        --cdc or an external CDC tool. Produce `rtl.cdc.crossings`,
        `rtl.cdc.unsynchronized_count`.

  3. FSM report:
        --fsm-extract. Produce `rtl.fsm.<name>.reachable_states`,
        `rtl.fsm.<name>.dead_states`.

  4. Optional sim:
        Verilator --binary + a directed testbench. Produce
        `rtl.sim.cycles_to_pass`, `rtl.sim.coverage.toggles_pct`, etc.

  5. Measurements use the `rtl.*` namespace so they never collide with
     the synth stage's `synth.*` keys. The verdict layer is already
     key-agnostic — once keys are published, no core changes needed.

Until implemented, from_existing() raises so a prediction against an
RTL question can never silently "pass" against an empty Reality.
"""
from __future__ import annotations

import json
import os

from agent.core import Reality, new_id
from agent.fix_verify import lint_counts, gather_rtl_files


# Verilator warning class -> rtl.lint.* measurement key. Several width
# subclasses fold into one key. Classes with no rule mapping are ignored.
_CLASS_TO_KEY: dict[str, str] = {
    "WIDTH":          "rtl.lint.width.count",
    "WIDTHEXPAND":    "rtl.lint.width.count",
    "WIDTHTRUNC":     "rtl.lint.width.count",
    "WIDTHCONCAT":    "rtl.lint.width.count",
    "LATCH":          "rtl.lint.latch.count",
    "BLKSEQ":         "rtl.lint.blkseq.count",
    "COMBDLY":        "rtl.lint.blkseq.count",
    "CASEINCOMPLETE": "rtl.lint.case_incomplete.count",
    "CASEOVERLAP":    "rtl.lint.full_case_pragma.count",
    "CASEX":          "rtl.lint.full_case_pragma.count",
    "IMPLICIT":       "rtl.lint.implicit_net.count",
    "MULTIDRIVEN":    "rtl.lint.multidriven.count",
    "UNOPTFLAT":      "rtl.lint.unoptflat.count",
}

# The measurement keys Verilator's lint genuinely COVERS — exactly the keys
# in _CLASS_TO_KEY. Only these are seeded to 0, where a 0 truthfully means
# "Verilator checked and found none." Categories Verilator does NOT analyze
# (CDC, RDC, FSM reachability/default, invented primitives, gated clocks, ...)
# are deliberately left ABSENT from the measurements, so a question about them
# grades as `unfalsifiable` ("the tool can't measure this") instead of a
# misleading 0 that reads as "no problem."
_VERILATOR_COVERED_KEYS: set[str] = set(_CLASS_TO_KEY.values())


class VerilatorRTLOracle:
    """RTL-stage oracle.

    Two modes today:

    - `from_findings_file`: replay a pre-computed `rtl_findings.json`
      whose top-level keys are already `rtl.*` measurement keys. This
      is what the rtl_demo scenario uses while a live Verilator binary
      isn't yet wired in. Lets us run the full agent loop end-to-end on
      RTL-stage questions today, with the same JSON shape any future
      live-Verilator path will produce.
    - `from_existing`: still a skeleton for live tool invocation.
      Will be filled in when Verilator-driven lint/CDC/FSM/sim land.
    """

    stage = "rtl"
    tool = "verilator"

    def __init__(self, *, top: str | None = None,
                 rtl_root: str | None = None,
                 reports_dir: str | None = None,
                 findings_path: str | None = None,
                 rtl_files: list[str] | None = None,
                 verilator_bin: str = "verilator",
                 source_label: str = "verilator"):
        self.top = top
        self.rtl_root = rtl_root
        self.reports_dir = reports_dir
        self.findings_path = findings_path
        if isinstance(rtl_files, str):
            rtl_files = [p.strip() for p in rtl_files.split(",") if p.strip()]
        self.rtl_files = list(rtl_files) if rtl_files else []
        self.verilator_bin = verilator_bin
        self.source_label = source_label

    def from_findings_file(self) -> Reality:
        if not self.findings_path or not os.path.exists(self.findings_path):
            raise FileNotFoundError(
                f"rtl_demo findings file not found: {self.findings_path}"
            )
        with open(self.findings_path) as f:
            measurements = json.load(f)
        # Sanity: every key must live under the rtl.* / sim.* / synth.*
        # namespaces used by the RTL pack. Reject anything else so
        # typos surface fast.
        bad = [k for k in measurements
               if not any(k.startswith(p) for p in
                          ("rtl.", "sim.", "synth.", "sta.", "power.",
                           "core.", "target.", "pdk.", "pnr.", "dft."))]
        if bad:
            raise ValueError(f"rtl findings file has out-of-namespace keys: {bad}")
        return Reality(
            id=new_id("real"),
            source=self.source_label,
            measurements=measurements,
            artifacts=[self.findings_path],
        )

    def from_live_lint(self) -> Reality:
        """Run `verilator --lint-only -Wall` over the RTL and turn the real
        warning counts into rtl.lint.* measurements (the answer key).

        -Wall is needed so hazard classes like BLKSEQ/LATCH are reported
        (default lint omits them). This is the TRUE reality — it can and
        does differ from hand-written findings files.
        """
        files = list(self.rtl_files)
        if not files and self.rtl_root and os.path.isdir(self.rtl_root):
            files = gather_rtl_files(self.rtl_root)
        if not files:
            raise FileNotFoundError(
                "VerilatorRTLOracle: no findings_path and no rtl_files/rtl_root "
                "to lint. Provide one so the oracle has an answer key."
            )
        classes = lint_counts(files, top=self.top,
                              verilator_bin=self.verilator_bin,
                              extra_args=["-Wall", "-Wno-fatal"])
        # Seed only Verilator-covered categories to 0 (real "checked, none
        # found"); leave everything else absent (-> unfalsifiable).
        measurements: dict = {k: 0 for k in _VERILATOR_COVERED_KEYS}
        for cls, n in classes.items():
            key = _CLASS_TO_KEY.get(cls)
            if key:
                measurements[key] = measurements.get(key, 0) + n
        return Reality(
            id=new_id("real"),
            source=f"{self.source_label}:live-lint",
            measurements=measurements,
            artifacts=list(files),
        )

    def from_existing(self) -> Reality:
        # A findings file (if it exists) is an explicit answer key; otherwise
        # run Verilator live to produce the real one.
        if self.findings_path and os.path.exists(self.findings_path):
            return self.from_findings_file()
        return self.from_live_lint()
