"""
cogni.adapters.rtl.verilator.perceiver
======================================

RTL-stage perceiver. Reads Verilog/SystemVerilog source and emits
WorldModel facts that an RTL-aware predictor can reason over BEFORE
synthesis (modules, ports, FSMs, pipeline depth, clock/reset domains,
combinational chains, etc.).

Status: SKELETON. The RTL adapter pair (perceiver here + oracle in
oracle.py) is the next major work item. This file exists so the
adapter registry has the right shape and so the team knows where the
code goes — it deliberately does NOT pretend to work.

What needs to land here:

  1. Parse RTL with a real frontend. Options, in order of preference:
     - Verilator's --xml-only output (battle-tested, free)
     - Slang's JSON AST (faster, modern SV support)
     - PyVerilog (pure-python, weakest SV support)
     Pick one, parse once into a normalized AST/IR, then derive facts.

  2. Emit RTL-level facts under the `rtl.*` namespace, e.g.:
        rtl.module.<name>.ports
        rtl.module.<name>.always_ff_count
        rtl.module.<name>.combinational_chain_depth_estimate
        rtl.fsm.<name>.state_count
        rtl.fsm.<name>.encoding (binary | onehot | gray | unknown)
        rtl.clock_domains
        rtl.reset_strategy (sync | async | mixed | none)
        rtl.lint.todo  (filled in by the oracle, perceiver only flags pre-lint)

  3. Tag facts so the rule engine can fire on RTL-stage rules:
     ["rtl_stage", "deep_combinational", "missing_reset", "wide_mux",
      "multi_clock", "fsm_unencoded", "blocking_in_seq", ...]

  4. Source attribution: every fact should carry the file:line it came
     from so KB rules can cite real provenance, not just "ibex_core.sv".

Until this is implemented, instantiating the adapter raises so we never
silently produce empty perceptions and confuse the verdict layer.
"""
from __future__ import annotations

import json
import os

from agent.core import WorldModel


class VerilatorRTLPerceiver:
    """RTL-stage perceiver.

    Two modes today:

    - `manifest_path` (when supplied): load a precomputed JSON
      describing the RTL (modules, FSMs, clock/reset, code_origin) and
      emit facts/tags directly. This is what scenarios/rtl_demo uses
      while a live Verilator parser isn't wired in.
    - `perceive`: live parsing path, still a skeleton — raises if no
      manifest_path is set.

    Both shapes write into the same `rtl.*` / `core.*` namespace so the
    rule engine doesn't care which mode produced the facts.
    """

    domain = "vlsi"
    stage = "rtl"
    tool = "verilator"

    def __init__(self, *, top: str | None = None,
                 include_dirs: list[str] | None = None,
                 defines: dict[str, str] | None = None,
                 manifest_path: str | None = None):
        self.top = top
        self.include_dirs = include_dirs or []
        self.defines = defines or {}
        self.manifest_path = manifest_path

    def perceive(self, world: WorldModel, raw_input: str) -> None:
        if self.manifest_path:
            self._perceive_from_manifest(world)
            return
        raise NotImplementedError(
            "VerilatorRTLPerceiver has no manifest_path and live parsing "
            "is not wired yet. Provide a manifest JSON or implement "
            "--xml-only parsing here. See module docstring."
        )

    def _perceive_from_manifest(self, world: WorldModel) -> None:
        if not os.path.exists(self.manifest_path):
            raise FileNotFoundError(f"rtl manifest not found: {self.manifest_path}")
        with open(self.manifest_path) as f:
            manifest = json.load(f)
        # Always attach the rtl_stage tag so v1 stage filters fire.
        # Per-fact tags can add more.
        source = manifest.get("source", self.manifest_path)
        facts = manifest.get("facts", {})
        tags = manifest.get("tags", [])
        for k, v in facts.items():
            world.add(k, v, source=source,
                      tags=tags if k == manifest.get("primary_key") else [])
        # Make sure rtl_stage is set even if the manifest forgot.
        world.tags.add("rtl_stage")
        for t in tags:
            world.tags.add(t)
