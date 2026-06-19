"""
cogni.adapters.synth.yosys.perceiver
=================
RTL perceiver adapter for Ibex-style RV32 cores. This is the *only*
file in the agent that knows what an SV file looks like.

It re-uses the parsing logic we wrote in cognipd/agent/ingest.py but
emits cogni-style Facts and tags.
"""
from __future__ import annotations
import os
import re
from agent.core import WorldModel


class IbexRTLAdapter:
    domain = "vlsi"
    stage = "synth"   # auto-tagged as `synth_stage` by Perceiver wrapper
    tool = "yosys"

    def perceive(self, world: WorldModel, raw_input: str):
        """raw_input is a path to an Ibex source tree root (containing rtl/)."""
        rtl_dir = os.path.join(raw_input, "rtl")
        if not os.path.isdir(rtl_dir):
            world.add(f"perception.error", f"no rtl/ at {raw_input}",
                      source="adapter:rtl_ibex", tags=["perception_error"])
            return

        # ---- Read ibex_pkg.sv for parameters ----
        pkg = os.path.join(rtl_dir, "ibex_pkg.sv")
        pkg_text = _read_or_empty(pkg)

        # multiplier configuration
        if "RV32MFast" in pkg_text or "RV32MFast" in _read_or_empty(os.path.join(rtl_dir, "ibex_core.sv")):
            world.add("core.multiplier", "FastMul (multi-cycle)",
                      source=pkg, tags=["has_multiplier", "multicycle_mul"])
        elif "RV32MSingleCycle" in pkg_text:
            world.add("core.multiplier", "SingleCycle",
                      source=pkg, tags=["has_multiplier", "singlecycle_mul"])
        else:
            world.add("core.multiplier", "M-extension present",
                      source=pkg, tags=["has_multiplier"])

        # Branch target ALU
        core_sv = _read_or_empty(os.path.join(rtl_dir, "ibex_core.sv"))
        if re.search(r"BranchTargetALU\s*=\s*1'b0", core_sv):
            world.add("core.bt_alu", False, source="ibex_core.sv", tags=["no_bt_alu"])
        else:
            world.add("core.bt_alu", "default-off", source="ibex_core.sv", tags=["no_bt_alu"])

        # Writeback stage
        if re.search(r"WritebackStage\s*=\s*1'b0", core_sv):
            world.add("core.wb_stage", False, source="ibex_core.sv",
                      tags=["no_wb_stage", "shallow_pipe", "single_writeback_path"])

        # PMP
        if re.search(r"PMPEnable\s*=\s*1'b0", core_sv):
            world.add("core.pmp", False, source="ibex_core.sv", tags=["no_pmp"])

        # ICache
        if re.search(r"ICache\s*=\s*1'b0", core_sv):
            world.add("core.icache", False, source="ibex_core.sv", tags=["no_icache"])

        # Register file: FF (default in ibex_core)
        if os.path.exists(os.path.join(rtl_dir, "ibex_register_file_ff.sv")):
            world.add("core.regfile", "FF (32x32)", source=os.path.join(rtl_dir, "ibex_register_file_ff.sv"),
                      tags=["has_register_file", "ff_regfile", "no_hard_macros"])

        # Reset style — Ibex uses synchronous reset by default
        if re.search(r"posedge\s+clk_i\s+or\s+negedge\s+rst_ni", core_sv):
            world.add("core.reset_style", "async-rst, sync-deassert",
                      source="ibex_core.sv", tags=["has_synchronous_reset"])

        # Pipeline depth (Ibex default = 2 stages without WB)
        world.add("core.pipeline_depth", 2, source="ibex_core.sv (default config)",
                  tags=["shallow_pipe"])

        # Generic small-core tag
        world.add("core.size_class", "small", source="adapter inference",
                  tags=["small_core"])

        # Single clock domain
        world.add("design.clock_domains", 1, source="ibex_core.sv (single clk_i)",
                  tags=["single_clock_domain"])

        # ---- v1-pack tags: synth-stage discriminators ----
        # Ibex is a flop-based RV32 core (no design intent for latches).
        # `intended_flop_based` lets r_synth_latch_warning_is_red_flag fire.
        world.tags.add("intended_flop_based")
        # The decoder + ALU + multdiv all do arithmetic / indexing, so the
        # width-warning rule applies.
        world.tags.add("has_arith_or_indexing")
        # Yosys generic synthesis maps to a generic-cell library; once the
        # netlist is in `synth.gate_counts`, downstream rules treat it as
        # stdcell_mapped (close enough for inv/buf-share heuristics).
        world.tags.add("stdcell_mapped")
        # FastMul is multicycle
        if "multicycle_mul" in world.tags:
            world.tags.add("multicycle_multiplier")
        # ibex_pkg + ibex_core have multi-master bus fabric (LSU + IF) and
        # heavy MUX usage in compressed-decoder + ALU. These tags let the
        # bus-fabric / mux-dominated rules fire.
        world.tags.add("bus_fabric")
        world.tags.add("mux_dominated")

        # ---- Read PDK / floorplan from optional config.mk if present ----
        # ORFS-style folders sometimes include this; otherwise leave to scenario config.
        for cfg_path in [os.path.join(raw_input, "config.mk"),
                         os.path.join(raw_input, "../config.mk")]:
            if os.path.exists(cfg_path):
                cfg = open(cfg_path).read()
                m = re.search(r"PLATFORM\s*=\s*(\S+)", cfg)
                if m:
                    plat = m.group(1)
                    world.add("design.platform", plat, source=cfg_path,
                              tags=[f"pdk_{plat.lower()}"])
                m = re.search(r"CORE_UTILIZATION\s*=\s*(\d+)", cfg)
                if m:
                    util = int(m.group(1))
                    world.add("design.core_utilization", util, source=cfg_path,
                              tags=[f"util_{util}"])
                m = re.search(r"clk_period\s*-period\s*([\d.]+)", cfg, re.I)
                if m:
                    period = float(m.group(1))
                    world.add("design.clock_period_ns", period, source=cfg_path,
                              tags=["clock_target_set"])
                break


def _read_or_empty(path: str) -> str:
    try:
        return open(path).read()
    except Exception:
        return ""
