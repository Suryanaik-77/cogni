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
import subprocess
import tempfile

from agent.core import WorldModel
from adapters.rtl.verilator.xml_facts import facts_from_xml


class VerilatorRTLPerceiver:
    """RTL-stage perceiver.

    Two modes:

    - LIVE (default): run Verilator ``--xml-only`` over the RTL source,
      parse the AST, and emit facts. Triggered when source files are given
      (via `rtl_files` or the `raw_input` path) and no manifest is set.
    - `manifest_path` (legacy/fixture): load a precomputed JSON describing
      the RTL and emit facts directly — used for offline tests or when
      Verilator isn't installed.

    Both modes write into the same `rtl.*` / `core.*` namespace so the rule
    engine doesn't care which produced the facts. The live parser reads
    only *structure* — lint warnings stay with the oracle (the answer key).
    """

    domain = "vlsi"
    stage = "rtl"
    tool = "verilator"

    def __init__(self, *, top: str | None = None,
                 include_dirs: list[str] | None = None,
                 defines: dict[str, str] | None = None,
                 manifest_path: str | None = None,
                 rtl_files: list[str] | None = None,
                 code_origin: str = "unknown",
                 author_intent: str | None = None,
                 verilator_bin: str = "verilator"):
        self.top = top
        self.include_dirs = include_dirs or []
        self.defines = defines or {}
        self.manifest_path = manifest_path
        # Accept a list, a single path, or a comma-separated string (the
        # flat-YAML config form). Anything falsy -> empty.
        if isinstance(rtl_files, str):
            rtl_files = [p.strip() for p in rtl_files.split(",") if p.strip()]
        self.rtl_files = list(rtl_files) if rtl_files else []
        self.code_origin = code_origin
        self.author_intent = author_intent
        self.verilator_bin = verilator_bin

    def perceive(self, world: WorldModel, raw_input: str) -> None:
        if self.manifest_path:
            self._perceive_from_manifest(world)
            return
        files = list(self.rtl_files)
        if raw_input:
            files.append(raw_input)
        if not files:
            raise ValueError(
                "VerilatorRTLPerceiver: no RTL source given. Pass rtl_files= "
                "or a source path as raw_input (or set manifest_path for the "
                "fixture path)."
            )
        self._perceive_from_verilator(world, files)

    # ------------------------------------------------------------------
    # Live Verilator path
    # ------------------------------------------------------------------

    def _run_verilator_xml(self, files: list[str]) -> str:
        """Invoke `verilator --xml-only` and return the AST XML text.

        `-Wno-lint` keeps lint warnings out of the way: the perceiver wants
        structure only; lint findings belong to the oracle.
        """
        out_dir = tempfile.mkdtemp(prefix="cogni_vxml_")
        out_xml = os.path.join(out_dir, "ast.xml")
        cmd = [self.verilator_bin, "--xml-only", "-Wno-lint",
               "--xml-output", out_xml]
        for inc in self.include_dirs:
            cmd.append("-I" + inc)
        for k, v in self.defines.items():
            cmd.append(f"+define+{k}={v}" if v else f"+define+{k}")
        if self.top:
            cmd += ["--top-module", self.top]
        cmd += files
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as e:
            raise RuntimeError(
                f"verilator not found ({self.verilator_bin!r}). Install it "
                f"(e.g. `dnf install verilator`) or use manifest_path."
            ) from e
        if not os.path.exists(out_xml):
            raise RuntimeError(
                "verilator --xml-only produced no XML.\n"
                f"command: {' '.join(cmd)}\nstderr:\n{proc.stderr}"
            )
        with open(out_xml) as f:
            return f.read()

    @staticmethod
    def _read_sources(files: list[str], max_chars: int = 16000) -> str:
        """Concatenate the RTL sources (each headed by its path) for the
        predictor to read. Bounded so a huge design can't blow up the prompt;
        truncation is marked explicitly."""
        chunks: list[str] = []
        total = 0
        for f in files:
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except OSError:
                continue
            header = f"// ===== {f} =====\n"
            chunk = header + body
            if total + len(chunk) > max_chars:
                chunk = chunk[: max(0, max_chars - total)] + "\n// ...[truncated]...\n"
                chunks.append(chunk)
                break
            chunks.append(chunk)
            total += len(chunk)
        return "\n".join(chunks).strip()

    def _perceive_from_verilator(self, world: WorldModel, files: list[str]) -> None:
        xml_text = self._run_verilator_xml(files)
        loc = 0
        for f in files:
            try:
                with open(f) as fh:
                    loc += sum(1 for _ in fh)
            except OSError:
                pass
        source = files[0] if len(files) == 1 else f"{files[0]} (+{len(files) - 1} more)"
        facts, tags = facts_from_xml(
            xml_text,
            source=source,
            lines_of_code=loc or None,
            code_origin=self.code_origin,
            author_intent=self.author_intent,
        )
        primary_key = "rtl.module.top"
        for k, v in facts.items():
            world.add(k, v, source=source,
                      tags=tags if k == primary_key else [])
        for t in tags:
            world.tags.add(t)
        world.tags.add("rtl_stage")

        # Expose the TOOL identity so tool-behavior rules can be recalled.
        # These facts come from Verilator, so learned rules gated on the tool
        # (e.g. "Verilator reports 0 latches on this pattern") match and reach
        # the predictor instead of being silently filtered out of recall.
        # Both forms are emitted because rules in the wild gate either way:
        # a `tool=verilator` fact and a `tool_verilator` tag.
        world.add("tool", "verilator", source=source)
        world.tags.add("tool_verilator")

        # Emit the raw source as a fact so the predictor can REASON from the
        # actual code the way a human reviewer does — counting cases, spotting
        # missing default arms, blocking assignments, suspect width compares.
        # This is NOT the lint answer key: the oracle runs Verilator lint as a
        # SEPARATE computation, so a latchy-LOOKING block Verilator scores as 0
        # still falsifies the prediction. Without this, the predictor only sees
        # aggregate counts and correctly refuses every count question.
        src_text = self._read_sources(files)
        if src_text:
            world.add("rtl.source", src_text, source=source)

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
