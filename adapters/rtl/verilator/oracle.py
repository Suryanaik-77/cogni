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
                 source_label: str = "verilator"):
        self.top = top
        self.rtl_root = rtl_root
        self.reports_dir = reports_dir
        self.findings_path = findings_path
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

    def from_existing(self) -> Reality:
        # If a findings file is supplied, prefer it.
        if self.findings_path:
            return self.from_findings_file()
        raise NotImplementedError(
            "VerilatorRTLOracle is a skeleton without findings_path. "
            "Pass findings_path to replay a precomputed lint dump, or "
            "wire a live verilator invocation in this method."
        )
