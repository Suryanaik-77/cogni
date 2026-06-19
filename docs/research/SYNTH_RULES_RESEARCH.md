# Synth-stage rule research dossier

This dossier proposes 24 candidate rules for the synth stage specifically: post-elaboration, post-mapping decisions and review gates, but pre-PnR signoff. The rules are tuned for open flows centered on Yosys plus ABC with OpenROAD/ORFS-style liberty mapping, while calling out where commercial tools usually behave differently ([Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html), [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html), [Ibex synthesis README](https://raw.githubusercontent.com/lowRISC/ibex/master/syn/README.md)).

The confidence labels below are about synth-stage predictive usefulness, not mathematical certainty. Several rules are strong because they recur across the current Ibex docs, ORFS design collateral, and recent open-flow postmortems; some are explicitly marked lower strength where the public evidence is thinner or where cross-node extrapolation is inherently noisy ([Ibex docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html), [Basilisk 2024](https://arxiv.org/html/2405.04257v2), [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)).

## (A) Empirical area scaling

### R-01: ibex_ff_regfile_area_band_by_pdk
- statement: For an Ibex-class small RV32 core with M extension, FF register file, and open-library standard-cell mapping, the synth-stage stdcell area lands in a surprisingly tight band by PDK: about 148k µm² on sky130hd, 32.5k µm² on Nangate45, and 2.43k µm² on ASAP7 in the maintained ORFS `ibex` collateral, which is a good first-pass anchor for post-elaboration sanity checks before floorplanning ([ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json), [ORFS nangate45 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/nangate45/ibex/rules-base.json), [ORFS ASAP7 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/asap7/ibex/rules-base.json)).
- kind: tendency
- strength: high
- applies_to.tools: [yosys, abc, openroad]
- applies_to.pdks: [sky130hd, nangate45, asap7]
- applies_to.design_class: [small_rv32_core]
- when: The core is roughly Ibex-sized, includes RV32M, keeps the register file in flip-flops, and does not replace major structures with SRAM macros.
- unless: The design uses RV32E, disables M, swaps in latch/SRAM-backed storage, adds large custom accelerators, or uses commercial synthesis with significantly more aggressive datapath optimization.
- predicts: [(synth.area_um2_stdcell, typical_range, "sky130hd 140k-180k; nangate45 30k-35k; asap7 2.3k-2.8k")] ([ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json), [ORFS nangate45 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/nangate45/ibex/rules-base.json), [ORFS ASAP7 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/asap7/ibex/rules-base.json)).
- prevents: Grossly undersized or oversized floorplans and misleading “this core exploded” triage loops; typical savings are 4-8 engineer-hours of avoidable PnR reruns.
- rationale: ORFS also publishes a sky130hd Ibex tutorial with roughly 191k µm² post-flow area on a 796×798 µm core, which is directionally consistent with the synth-stage sky130 anchor once downstream buffering and placement overhead are added ([ORFS Ibex tutorial](https://openroad-flow-scripts.readthedocs.io/en/latest/tutorials/FlowTutorial.html), [ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json)).
- citations: [ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json); [ORFS nangate45 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/nangate45/ibex/rules-base.json); [ORFS ASAP7 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/asap7/ibex/rules-base.json); [ORFS Ibex tutorial](https://openroad-flow-scripts.readthedocs.io/en/latest/tutorials/FlowTutorial.html)

### R-02: single_cycle_multiplier_is_the_biggest_area_step
- statement: In Ibex-class cores, enabling the single-cycle multiplier is usually the most expensive single synth-stage architectural switch: the CARRV Ibex study measured about +15% area at 100 MHz and +33% area at max frequency for SC-Mult, while the current Ibex docs say the single-cycle implementation is expected to consume roughly 3-4× the fast multiplier’s ASIC area because it instantiates three parallel 17×17 multiplier units ([CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf), [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)).
- kind: tendency
- strength: high
- applies_to.tools: [yosys, abc, dc, genus]
- applies_to.pdks: [sky130hd, nangate45, asap7, any]
- applies_to.design_class: [small_rv32_core, accelerator]
- when: The multiplier is kept combinational or near-combinational within one architectural cycle.
- unless: The design maps multiplication to dedicated hard macros or DSP blocks, or the multiplier is heavily time-multiplexed.
- predicts: [(synth.area_um2_stdcell, delta_vs_fast_mult, "+15% to +33% typical on Ibex-class cores"), (synth.top_module_by_cells, likely, "multdiv/mult")] ([CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf), [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)).
- prevents: Under-budgeting area for RV32M bring-up and discovering congestion too late; typical savings are 0.5-1 day of back-and-forth across RTL and floorplanning.
- rationale: The same CARRV data also shows SC-Mult barely changed max frequency, which means the area jump is real but often does not buy a proportional Fmax benefit in small in-order cores ([CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf)).
- citations: [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf); [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)

### R-03: multicycle_multiplier_buys_area_with_latency
- statement: Fast or slow multicycle multipliers are the default area-efficient choice for ASIC-like open flows: the current Ibex docs call `RV32MFast` the first choice for ASIC synthesis and put it at 3-4 cycles, while `RV32MSlow` stretches MUL to roughly `clog2(op_b)+1` cycles and MULH to 33 cycles, explicitly trading latency for a smaller block than the single-cycle option ([Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html), [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf)).
- kind: heuristic
- strength: high
- applies_to.tools: [yosys, abc, dc, genus]
- applies_to.pdks: [sky130hd, nangate45, asap7, any]
- applies_to.design_class: [small_rv32_core, accelerator]
- when: Throughput is not hard-bound to one multiply result per cycle.
- unless: The product must retire in a single cycle or a dedicated macro already provides the multiply datapath efficiently.
- predicts: [(synth.area_um2_stdcell, delta_vs_single_cycle, "lower by roughly mid-teens to low-thirties percent on Ibex-class cores"), (synth.mul_latency_cycles, typical, "fast 3-4; slow 2-33 depending on op")] ([Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html), [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf)).
- prevents: Chasing single-cycle RV32M for “free performance” and then paying for it in area and routeability; typical savings are 4-12 engineer-hours during architecture selection.
- rationale: Public Ibex guidance and the CARRV measurements point in the same direction: the architectural knob changes area far more reliably than it improves the core’s true critical path or achievable clock ([Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html), [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf)).
- citations: [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html); [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf)

### R-04: ff_regfile_has_a_large_area_floor_so_macro_or_latch_backing_pays
- statement: A 31×32 FF register file already carries a large synth-stage storage floor before read muxing is counted: using representative public DFF areas, the storage bits alone are about 24.8k µm² in sky130hd-equivalent cells, 4.49k µm² in Nangate45, and 289 µm² in ASAP7, which explains why the Ibex docs steer ASIC users toward latch-based storage and FPGA users toward RAM-backed storage ([Sky130 DFF liberty JSON](https://foss-eda-tools.googlesource.com/skywater-pdk/libs/sky130_fd_sc_hd/+/refs/heads/main/cells/dfrtp/sky130_fd_sc_hd__dfrtp_1__tt_025C_1v80.lib.json), [Nangate45 typical liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib), [ASAP7 sequential liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/asap7/lib/NLDM/asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib), [Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)).
- kind: identity
- strength: high
- applies_to.tools: [yosys, abc, openroad, dc, genus]
- applies_to.pdks: [sky130hd, nangate45, asap7, any]
- applies_to.design_class: [small_rv32_core, accelerator]
- when: The architectural register file is synthesized into ordinary flops.
- unless: The design uses a latch-based regfile, SRAM macro, or custom multiported memory compiler.
- predicts: [(synth.regfile_storage_floor_um2, lower_bound, "sky130hd ~24.8k; nangate45 ~4.5k; asap7 ~0.29k"), (synth.area_um2_stdcell, delta_if_replaced, "material drop once FF regfile is removed from stdcell budget")] ([Sky130 DFF liberty JSON](https://foss-eda-tools.googlesource.com/skywater-pdk/libs/sky130_fd_sc_hd/+/refs/heads/main/cells/dfrtp/sky130_fd_sc_hd__dfrtp_1__tt_025C_1v80.lib.json), [Nangate45 typical liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib), [ASAP7 sequential liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/asap7/lib/NLDM/asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib), [Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)).
- prevents: Misattributing a large core area share to “control logic” when the regfile choice is the real driver; typical savings are 4-8 engineer-hours of misplaced optimization work.
- rationale: The Ibex docs report that the FPGA RAM-backed variant saved about 600 LUTs and 1000 FFs on Arty A7 versus the FF version, while the latch-based variant is called the first choice for ASIC precisely because the storage decision dominates area early ([Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)).
- citations: [Sky130 DFF liberty JSON](https://foss-eda-tools.googlesource.com/skywater-pdk/libs/sky130_fd_sc_hd/+/refs/heads/main/cells/dfrtp/sky130_fd_sc_hd__dfrtp_1__tt_025C_1v80.lib.json); [Nangate45 typical liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib); [ASAP7 sequential liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/asap7/lib/NLDM/asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib); [Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)

### R-05: fixed_topology_bus_fabrics_scale_roughly_with_data_width
- statement: For mux- and steer-dominated bus fabrics, doubling data width usually pushes synth cell count by roughly 1.7-2.3× rather than remaining flat, because the steering logic is largely bit-sliced even after Yosys collapses duplicated mux inputs; the open-flow evidence is indirect but consistent, combining Yosys’ documented mux-reduction behavior with recent reports that poor part-select lowering can explode mux trees when width-dependent selection logic is mishandled ([Yosys opt_reduce docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html), [Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- kind: heuristic
- strength: medium
- applies_to.tools: [yosys, abc, dc, genus]
- applies_to.pdks: [any]
- applies_to.design_class: [bus_fabric, accelerator]
- when: The topology, arbitration policy, and number of endpoints stay fixed while data width changes.
- unless: The width change also changes buffering, pipelining, protocol logic, or the synthesis tool recognizes and rewrites the structure more aggressively than Yosys typically does.
- predicts: [(synth.cell_count_stdcell, scaling, "~1.7-2.3x for 2x data width at fixed topology"), (synth.area_um2_stdcell, scaling, "near-linear with width for mux-dominated fabrics")] ([Yosys opt_reduce docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html), [Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- prevents: Assuming a 32→64 bit fabric bump is “just wires” and blowing up synth-area or route congestion later; typical savings are 4-8 engineer-hours in early architectural sizing.
- rationale: The exact multiplier depends on control and arbitration overhead, but the public Yosys material makes clear that mux structure and part-select lowering are first-order QoR determinants in bit-sliced fabrics ([Yosys opt_reduce docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html), [Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- citations: [Yosys opt_reduce docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html); [Basilisk 2024](https://arxiv.org/html/2405.04257v2)

### R-06: dsp_style_blocks_are_multiply_dominated_and_width_growth_is_superlinear
- statement: DSP-style integer datapaths show a stable pattern across recent public evidence: adder width sweeps look relatively well-behaved, but multiplier cost and delay escalate much faster with width, and pipelining or decomposition mainly shifts the area-delay Pareto rather than removing the multiplier as the dominant block ([OpenROAD ALU discussion](https://github.com/The-OpenROAD-Project/OpenROAD/discussions/3881), [2024 multiplier tradeoff paper](https://arxiv.org/html/2407.03962v1)).
- kind: tendency
- strength: medium
- applies_to.tools: [yosys, abc, dc, genus]
- applies_to.pdks: [sky130hd, asap7, any]
- applies_to.design_class: [dsp, accelerator]
- when: The block is arithmetic-heavy and multiplier-rich.
- unless: The design maps into dedicated DSP macros, uses aggressive multi-cycle scheduling, or is dominated by memory rather than arithmetic.
- predicts: [(synth.area_um2_stdcell, scaling, "adder-like paths near-linear with width; multiply-like paths superlinear"), (synth.critical_path_block, likely, "multiplier or multiplier-adjacent reduction tree")] ([OpenROAD ALU discussion](https://github.com/The-OpenROAD-Project/OpenROAD/discussions/3881), [2024 multiplier tradeoff paper](https://arxiv.org/html/2407.03962v1)).
- prevents: Over-optimizing adders while the multiplier tree remains the true size and timing limiter; typical savings are 4-12 engineer-hours on DSP microarchitecture selection.
- rationale: The 2024 multiplier study explicitly reports new Pareto points from decomposition and pipelining, not the disappearance of the fundamental area-delay tradeoff, while the OpenROAD ALU experiments show the relative behavior is similar across sky130hd and ASAP7 ([2024 multiplier tradeoff paper](https://arxiv.org/html/2407.03962v1), [OpenROAD ALU discussion](https://github.com/The-OpenROAD-Project/OpenROAD/discussions/3881)).
- citations: [OpenROAD ALU discussion](https://github.com/The-OpenROAD-Project/OpenROAD/discussions/3881); [2024 multiplier tradeoff paper](https://arxiv.org/html/2407.03962v1)

## (B) Cell-count distribution

### R-07: ff_share_is_often_in_the_low_teens_even_before_pipeline_state
- statement: In a small Ibex-class core, the FF share of total mapped cells is often in the low teens to around 20%, and there is a strong lower-bound argument from the register file alone: 992 architectural bits of 31×32 storage account for about 16.8% of ORFS sky130hd Ibex synth area, 13.8% of Nangate45 synth area, and 11.9% of ASAP7 synth area before adding pipeline registers, CSRs, and control flops ([Sky130 DFF liberty JSON](https://foss-eda-tools.googlesource.com/skywater-pdk/libs/sky130_fd_sc_hd/+/refs/heads/main/cells/dfrtp/sky130_fd_sc_hd__dfrtp_1__tt_025C_1v80.lib.json), [Nangate45 typical liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib), [ASAP7 sequential liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/asap7/lib/NLDM/asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib), [ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json), [ORFS nangate45 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/nangate45/ibex/rules-base.json), [ORFS ASAP7 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/asap7/ibex/rules-base.json), [Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)).
- kind: tendency
- strength: high
- applies_to.tools: [yosys, abc, dc, genus]
- applies_to.pdks: [sky130hd, nangate45, asap7, any]
- applies_to.design_class: [small_rv32_core]
- when: The regfile is FF-based and the core is a simple in-order RV32 design.
- unless: The regfile is macro-backed, the design is heavily memory- or accelerator-dominated, or the ISA/state set is unusually small.
- predicts: [(synth.ff_share_pct, typical_range, "~10-20%"), (synth.ff_share_pct, lower_bound_from_regfile_only, "~11.9-16.8% across the three public libraries")] ([Sky130 DFF liberty JSON](https://foss-eda-tools.googlesource.com/skywater-pdk/libs/sky130_fd_sc_hd/+/refs/heads/main/cells/dfrtp/sky130_fd_sc_hd__dfrtp_1__tt_025C_1v80.lib.json), [ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json), [Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)).
- prevents: Wasting effort hunting “mystery combinational bloat” when the sequential footprint is structurally normal; typical savings are 2-6 engineer-hours in report triage.
- rationale: This rule is stronger than a generic textbook estimate because it is anchored to actual public DFF areas and actual public ORFS Ibex area budgets, not just architectural intuition ([Sky130 DFF liberty JSON](https://foss-eda-tools.googlesource.com/skywater-pdk/libs/sky130_fd_sc_hd/+/refs/heads/main/cells/dfrtp/sky130_fd_sc_hd__dfrtp_1__tt_025C_1v80.lib.json), [ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json)).
- citations: [Sky130 DFF liberty JSON](https://foss-eda-tools.googlesource.com/skywater-pdk/libs/sky130_fd_sc_hd/+/refs/heads/main/cells/dfrtp/sky130_fd_sc_hd__dfrtp_1__tt_025C_1v80.lib.json); [Nangate45 typical liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib); [ASAP7 sequential liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/asap7/lib/NLDM/asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib); [ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json); [ORFS nangate45 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/nangate45/ibex/rules-base.json); [ORFS ASAP7 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/asap7/ibex/rules-base.json); [Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)

### R-08: multdiv_is_usually_the_largest_leaf_once_m_is_enabled
- statement: In an Ibex-like RV32IM core, the mult/div block is usually the top module by cells once M is enabled, because the public data says multiplier choice is the single biggest area knob and the upstream docs explicitly describe large internal multiplier structures for both single-cycle and fast variants ([CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf), [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)).
- kind: tendency
- strength: high
- applies_to.tools: [yosys, abc, dc, genus]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core]
- when: RV32M is present and no other large accelerator or memory macro shares the same top-level budget.
- unless: RV32M is disabled, multiplication is offloaded, or another custom block dominates the leaf hierarchy.
- predicts: [(synth.top_module_by_cells, likely, "multdiv or multiplier-related leaf")] ([CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf), [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)).
- prevents: Optimizing decode or CSR logic first when the real cell hotspot is the arithmetic leaf; typical savings are 2-6 engineer-hours of misplaced module-level effort.
- rationale: The evidence is not a published per-module cell histogram, but the area deltas are large enough that leaf dominance is the practical default assumption unless a custom block obviously overrides it ([CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf), [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)).
- citations: [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf); [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)

### R-09: inv_buf_tail_is_nontrivial_and_often_means_pressure
- statement: A noticeable INV/BUF tail after technology mapping is normal in open flows, because `abc` can insert buffers during constrained mapping, `dfflibmap` may add inverters around FF legalization, and ORFS even exposes `REMOVE_ABC_BUFFERS` as a dedicated knob; if INV/BUF share is already clearly into double digits pre-PnR, treat it as a pressure signal rather than harmless noise ([Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html), [Yosys dfflibmap docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.47/cmd/dfflibmap.html), [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html), [Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- kind: heuristic
- strength: medium
- applies_to.tools: [yosys, abc, openroad]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core, bus_fabric, accelerator, any]
- when: The target period is nontrivial or the design has broad fanout or awkward FF cell matching.
- unless: The library itself strongly biases inverter insertion or the flow immediately strips ABC buffers before later stages.
- predicts: [(synth.invbuf_share_pct, review_gate, ">10% suggests timing/fanout pressure worth inspection"), (synth.buffer_count, direction, "rises with tighter constraints and weaker hierarchy/QoR")] ([Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html), [Yosys dfflibmap docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.47/cmd/dfflibmap.html), [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html)).
- prevents: Dismissing a real timing/fanout problem as “just buffer fluff” and pushing avoidable congestion into PnR; typical savings are 2-8 engineer-hours of debug.
- rationale: Basilisk’s recent postmortem is especially relevant here because it ties Yosys netlist reduction to very high-fanout nets, which is exactly the environment in which buffer tails stop being benign ([Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- citations: [Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html); [Yosys dfflibmap docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.47/cmd/dfflibmap.html); [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html); [Basilisk 2024](https://arxiv.org/html/2405.04257v2)

### R-10: icg_count_stays_near_zero_without_explicit_insertion
- statement: In open Yosys+ABC ASIC flows, clock-gating insertion rate is usually near zero unless the RTL or wrapper library already instantiates it, because ORFS exposes clock-gating cells as black boxes rather than an aggressive automatic insertion flow, and Ibex’s own latch-based register file requires a technology-specific `prim_clock_gating` wrapper to be provided explicitly ([OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html), [Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)).
- kind: identity
- strength: high
- applies_to.tools: [yosys, abc, openroad]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core, dsp, accelerator, bus_fabric, any]
- when: The flow is plain Yosys+ABC without a power-intent-aware commercial gating pass.
- unless: ICG cells are instantiated in RTL, wrapped in generated IP, or inserted by a downstream proprietary flow.
- predicts: [(synth.icg_per_1kff, typical_range, "~0 unless explicitly instantiated")] ([OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html), [Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)).
- prevents: Assuming automatic clock gating will rescue dynamic power or fanout later; typical savings are 2-4 engineer-hours by making the power strategy explicit earlier.
- rationale: This is one of the clearest practical differences from commercial synthesis, where automated clock-gating insertion is far more common and far less manual ([Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html), [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html)).
- citations: [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html); [Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)

## (C) Critical-path levels and WNS predictors

### R-11: default_ibex_wns_is_usually_not_set_by_the_multiplier
- statement: In Ibex-class cores, the worst path is more likely to come from control and instruction-request logic than from the multiplier itself: the CARRV study found that SC-Mult and WB-Stage raised area without materially hurting max frequency, while SBP and BT-ALU cut max frequency by 22% and 16% because they extend the instruction-memory-request side of the machine ([CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf), [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)).
- kind: tendency
- strength: high
- applies_to.tools: [yosys, abc, dc, genus]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core]
- when: The multiplier is multicycle or otherwise not on the single-cycle retire boundary.
- unless: A large combinational multiplier or custom arithmetic block is reintroduced into the critical architectural cycle.
- predicts: [(synth.critical_path_block, likely, "control / branch / instruction-request cone rather than multiplier")] ([CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf), [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)).
- prevents: Spending timing effort in the wrong module hierarchy and leaving the real control cone untouched; typical savings are 4-8 engineer-hours.
- rationale: This is valuable precisely because the intuition “biggest block equals worst path” fails here; public Ibex evidence shows area dominance and critical-path dominance can diverge ([CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf)).
- citations: [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf); [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)

### R-12: multicycle_paths_convert_comb_depth_into_architectural_latency
- statement: Turning a long arithmetic function into a multicycle path is one of the few synth-stage levers that predictably reduces immediate combinational depth, because the Ibex fast and slow multipliers explicitly spread work across 3-4 cycles or up to 33 cycles, shifting the performance cost into CPI rather than forcing a single-cycle WNS fight ([Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html), [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf)).
- kind: identity
- strength: high
- applies_to.tools: [yosys, abc, dc, genus]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core, dsp, accelerator]
- when: The workload tolerates extra cycles on the arithmetic operation.
- unless: The design contract requires one-cycle completion or the multicycle declaration is only a constraints trick without architectural restructuring.
- predicts: [(synth.wns_ns, direction, "improves versus single-cycle equivalent"), (synth.op_latency_cycles, direction, "worsens by the chosen multicycle count")] ([Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html), [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf)).
- prevents: Trying to rescue too-deep arithmetic with sizing alone when the real fix is architectural staging; typical savings are 4-12 engineer-hours.
- rationale: This rule is stronger for synth-stage triage than a generic “pipeline more” recommendation because the Ibex docs quantify the cycle shifts for each variant ([Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)).
- citations: [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html); [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf)

### R-13: adder_mux_adder_chains_are_open_flow_wns_hazards
- statement: In open synthesis, operator chains such as adder→mux→adder are reliable WNS hazards because Yosys has dedicated passes to rebalance cascaded `$add`, `$mul`, and mux structures for timing, and recent open-flow work shows that inefficient part-select and mux lowering can create far larger combinational cones than the RTL author intended ([Yosys opt_balance_tree docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html), [Yosys opt_reduce docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html), [Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- kind: heuristic
- strength: medium
- applies_to.tools: [yosys, abc, dc, genus]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core, dsp, accelerator, bus_fabric]
- when: A single cycle contains chained arithmetic plus conditional steering.
- unless: The cone is already explicitly staged or a commercial datapath optimizer rewrites it more aggressively.
- predicts: [(synth.wns_ns, risk_signal, "negative slack more likely under tight periods"), (synth.path_signature, likely, "arithmetic + steering chain")] ([Yosys opt_balance_tree docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html), [Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- prevents: Treating a deep arithmetic/steering cone as a placement problem only; typical savings are 2-8 engineer-hours by inserting a register or restructuring earlier.
- rationale: The important insight is not that these chains are “bad” in theory, but that open tools document extra passes specifically to mitigate them, which means they recur often enough to deserve a review rule ([Yosys opt_balance_tree docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html), [Yosys opt_reduce docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html)).
- citations: [Yosys opt_balance_tree docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html); [Yosys opt_reduce docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html); [Basilisk 2024](https://arxiv.org/html/2405.04257v2)

### R-14: high_fanout_control_nets_predict_bad_wns_better_than_leaf_size_does
- statement: High-fanout control and decode nets are strong WNS predictors in Yosys-based flows because Basilisk reports that Yosys reduces redundant logic without considering timing, fanout, or placement, thereby creating very high-fanout nets, while ABC only partially corrects the situation using liberty timing and max-fanout assumptions rather than placement-aware optimization ([Basilisk 2024](https://arxiv.org/html/2405.04257v2), [Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html), [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)).
- kind: tendency
- strength: high
- applies_to.tools: [yosys, abc, openroad]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core, bus_fabric, accelerator, any]
- when: The design contains broad control distribution, wide decode, or shared enables/selects.
- unless: The cone is physically partitioned early or downstream commercial synthesis/place tools are allowed to clone and retime aggressively.
- predicts: [(synth.wns_ns, risk_signal, "degrades as fanout and broad control sharing rise"), (synth.buffer_count, direction, "rises later in repair flows")] ([Basilisk 2024](https://arxiv.org/html/2405.04257v2), [Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html)).
- prevents: Focusing only on datapath depth while broad shared controls quietly drive post-synth and post-place timing pain; typical savings are 4-10 engineer-hours.
- rationale: ORFS exposes default driver and load assumptions precisely because these electrical surrogates matter when the netlist itself was not built with placement awareness ([OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html), [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)).
- citations: [Basilisk 2024](https://arxiv.org/html/2405.04257v2); [Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html); [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html); [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)

## (D) Yosys/ABC-specific behaviors

### R-15: flattening_improves_qor_but_costs_runtime_and_structure
- statement: Flat synthesis is usually the best pure-QoR setting in Yosys because flattening removes module boundaries and allows maximum optimization, but the OpenROAD maintainers say it also gives the largest runtime and loses hierarchy that is useful to both users and downstream physical planning ([OpenROAD hierarchy discussion](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/discussions/1647), [Yosys flatten docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_hierarchy.html)).
- kind: heuristic
- strength: high
- applies_to.tools: [yosys, abc, openroad]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core, dsp, accelerator, bus_fabric, any]
- when: Cross-module logic sharing or timing across boundaries matters more than runtime and inspectability.
- unless: The hierarchy itself is needed for macro planning, debug, or runtime containment.
- predicts: [(synth.area_um2_stdcell, direction, "usually lower or equal when flattened"), (synth.runtime_s, direction, "higher when flattened")] ([OpenROAD hierarchy discussion](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/discussions/1647), [Yosys flatten docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_hierarchy.html)).
- prevents: Running full flat synthesis blindly on every iteration and paying avoidable compile-time tax; typical savings are 4-12 engineer-hours over repeated iteration.
- rationale: Yosys’ own `keep_hierarchy` and flatten controls exist because this is not a cosmetic choice; it is a first-order QoR/runtime tradeoff ([Yosys flatten docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_hierarchy.html)).
- citations: [OpenROAD hierarchy discussion](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/discussions/1647); [Yosys flatten docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_hierarchy.html)

### R-16: the_best_open_flow_compromise_is_often_two_pass_hierarchy_tuning
- statement: The most practical open-flow compromise is often a two-pass scheme that first measures module sizes hierarchically and then flattens small modules while preserving larger ones, because the ORFS maintainers explicitly describe that as their workaround for Yosys’ current difficulty in choosing the right keep list automatically ([OpenROAD hierarchy discussion](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/discussions/1647), [Yosys keep_hierarchy docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_hierarchy.html), [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html)).
- kind: heuristic
- strength: high
- applies_to.tools: [yosys, abc, openroad]
- applies_to.pdks: [any]
- applies_to.design_class: [accelerator, bus_fabric, any]
- when: The design is large enough that full flattening is expensive but pure hierarchy gives visibly worse QoR.
- unless: The design is small enough to flatten cheaply or already forced into black-box partitions.
- predicts: [(synth.runtime_s, direction, "better than full flatten"), (synth.area_um2_stdcell, direction, "better than pure hierarchy"), (synth.wns_ns, direction, "better than pure hierarchy")] ([OpenROAD hierarchy discussion](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/discussions/1647), [Yosys keep_hierarchy docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_hierarchy.html)).
- prevents: Binary “flat or hierarchical” thinking that leaves a lot of open-flow QoR on the table; typical savings are 4-12 engineer-hours.
- rationale: ORFS’ published variables for hierarchical synthesis, keep-module lists, and minimum area thresholds show that this is an active design-space knob, not an edge-case hack ([OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html)).
- citations: [OpenROAD hierarchy discussion](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/discussions/1647); [Yosys keep_hierarchy docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_hierarchy.html); [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html)

### R-17: for_asic_standard_cells_use_abc_not_abc9_as_the_default_assumption
- statement: For standard-cell ASIC synthesis in current public Yosys flows, `abc` is the default assumption and `abc9` is not the baseline answer: the upstream docs describe `abc9` primarily in FPGA terms and require fully selected modules, while ORFS exposes standard `ABC_AREA` and `ABC_SPEED` style knobs rather than an ASIC `abc9` recipe ([Yosys ABC/ABC9 docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html), [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html)).
- kind: constraint
- strength: medium
- applies_to.tools: [yosys, abc, abc9, openroad]
- applies_to.pdks: [sky130hd, nangate45, asap7]
- applies_to.design_class: [small_rv32_core, dsp, accelerator, bus_fabric, any]
- when: The flow is mapping to liberty-described standard cells.
- unless: A custom research flow explicitly demonstrates `abc9` or `abc_new` benefits on the target library.
- predicts: [(synth.mapping_engine, default, "abc with liberty constraints"), (synth.qor_risk, warning, "abc9 assumptions often misapplied in ASIC discussions")] ([Yosys ABC/ABC9 docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html), [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html)).
- prevents: Benchmarking the wrong mapper or assuming FPGA-focused docs transfer cleanly into ASIC liberty mapping; typical savings are 2-6 engineer-hours.
- rationale: The 2024 PULP talk reinforces this by framing open synthesis work around better control of `abc` flows and next-generation mappers, not around `abc9` as an already-settled ASIC solution ([PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)).
- citations: [Yosys ABC/ABC9 docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html); [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html); [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)

### R-18: opt_clean_opt_share_and_balance_tree_are_helpful_but_local
- statement: Yosys’ cleanup and sharing passes help, but they are local and conditional rather than magical: `opt_clean` removes unused wires and cells, `opt_hier` only propagates constants and ties across boundaries, `opt_share` only appears in full optimization flow, and `opt_balance_tree` rewrites cascaded arithmetic/logic trees for timing rather than inventing new architectural cuts ([Yosys opt docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_opt.html), [Yosys detailed opt docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html)).
- kind: constraint
- strength: medium
- applies_to.tools: [yosys]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core, dsp, accelerator, bus_fabric, any]
- when: The QoR issue is structural duplication, dead logic, or an obvious unbalanced tree.
- unless: The real issue is missing pipeline stages, poor hierarchy partitioning, or a need for full-chip retiming/cloning.
- predicts: [(synth.area_um2_stdcell, direction, "incremental improvement, not architectural collapse"), (synth.wns_ns, direction, "incremental improvement on cascades")] ([Yosys opt docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_opt.html), [Yosys detailed opt docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html)).
- prevents: Expecting Yosys pass ordering alone to fix a pipeline-depth problem; typical savings are 2-6 engineer-hours.
- rationale: Recent open-flow gap analyses still call out missing retiming, cloning, and deeper timing-awareness, which is the clearest sign that local pass tuning has limits ([OpenROAD MegaBoom issue](https://github.com/The-OpenROAD-Project/OpenROAD/issues/4623), [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)).
- citations: [Yosys opt docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_opt.html); [Yosys detailed opt docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html); [OpenROAD MegaBoom issue](https://github.com/The-OpenROAD-Project/OpenROAD/issues/4623); [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)

## (E) Warnings as predictive signals

### R-19: latch_warning_is_a_pre_sta_red_flag_unless_intentional
- statement: A latch warning should stop normal synth review unless the latch is intentional, because Verilator documents that incomplete assignment in a combinational block infers a latch, and Yosys’ `proc_dlatch` pass explicitly identifies such latches and converts them into d-type latches, which then bring level-sensitive timing behavior into a flow that is otherwise mostly edge-triggered ([Verilator warnings guide](https://verilator.org/guide/latest/warnings.html), [Yosys proc_dlatch docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.43/cmd/proc_dlatch.html)).
- kind: constraint
- strength: high
- applies_to.tools: [yosys, verilator, dc, genus]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core, dsp, accelerator, bus_fabric, any]
- when: The design is intended to be flop-based.
- unless: The latch is deliberate and reviewed as such, as in a latch-based regfile or explicit `always_latch` coding style.
- predicts: [(synth.warning_count.latch, review_gate, ">0 requires explicit waiver or fix"), (synth.sta_risk, likely, "level-sensitive timing hazard")] ([Verilator warnings guide](https://verilator.org/guide/latest/warnings.html), [Yosys proc_dlatch docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.43/cmd/proc_dlatch.html)).
- prevents: Silent introduction of transparent timing paths that surface later as confusing STA failures; typical savings are 4-10 engineer-hours.
- rationale: This is one place where “it simulates” is not an adequate defense, because the synth product is a different timing object than the intended flop-based design ([Verilator warnings guide](https://verilator.org/guide/latest/warnings.html)).
- citations: [Verilator warnings guide](https://verilator.org/guide/latest/warnings.html); [Yosys proc_dlatch docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.43/cmd/proc_dlatch.html)

### R-20: widthtrunc_warning_is_both_a_functional_and_qor_signal
- statement: Width and truncation warnings are not mere lint cosmetics in synth review, because Verilator documents them as real size mismatches that should be fixed explicitly, and Basilisk shows that poor part-select lowering in open synthesis can create much larger mux structures than necessary when width-dependent selection semantics are mishandled ([Verilator warnings guide](https://verilator.org/guide/latest/warnings.html), [Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- kind: constraint
- strength: high
- applies_to.tools: [yosys, verilator, dc, genus]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core, dsp, accelerator, bus_fabric, any]
- when: Arithmetic, indexing, slicing, or concatenation is involved.
- unless: The truncation is deliberate, documented, and forced with explicit casts or slices.
- predicts: [(synth.warning_count.width, review_gate, ">0 requires explicit intent"), (synth.area_um2_stdcell, direction, "can rise indirectly through fixup or oversized muxing")] ([Verilator warnings guide](https://verilator.org/guide/latest/warnings.html), [Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- prevents: Late functional escapes and unnecessary datapath bloat from accidental width growth; typical savings are 4-12 engineer-hours.
- rationale: Width mismatches are one of the rare warning classes that can be both a correctness problem and a QoR problem at the same time ([Verilator warnings guide](https://verilator.org/guide/latest/warnings.html), [Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- citations: [Verilator warnings guide](https://verilator.org/guide/latest/warnings.html); [Basilisk 2024](https://arxiv.org/html/2405.04257v2)

### R-21: multidriven_warning_is_an_immediate_stop_review_signal
- statement: A multidriven warning is almost always a bug-level event at synth review, because Yosys `check` flags conflicting drivers as a core correctness problem and Verilator warns that multiple driving blocks with different clocking can create CDC or timing bugs even if simulation still runs ([Yosys check docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_status.html), [Verilator warnings guide](https://verilator.org/guide/latest/warnings.html)).
- kind: constraint
- strength: high
- applies_to.tools: [yosys, verilator, dc, genus]
- applies_to.pdks: [any]
- applies_to.design_class: [small_rv32_core, dsp, accelerator, bus_fabric, any]
- when: A net or register is driven from more than one process or clocking context.
- unless: The pattern is a consciously reviewed special structure and the tool-specific semantics are known to be safe, which is rare.
- predicts: [(synth.warning_count.multidriven, review_gate, ">0 is a stop-ship style review item")] ([Yosys check docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_status.html), [Verilator warnings guide](https://verilator.org/guide/latest/warnings.html)).
- prevents: Burning cycles on QoR work before basic single-driver correctness is restored; typical savings are 2-8 engineer-hours plus bug-escape risk.
- rationale: This is one of the few warning classes where the correct operational response is usually “halt the review” rather than “note it and continue” ([Yosys check docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_status.html), [Verilator warnings guide](https://verilator.org/guide/latest/warnings.html)).
- citations: [Yosys check docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_status.html); [Verilator warnings guide](https://verilator.org/guide/latest/warnings.html)

## (F) PPA trade-offs known empirically

### R-22: tightening_target_delay_has_a_convex_cost_curve
- statement: Tightening target delay in open flows usually gives a convex area/runtime tradeoff rather than a linear one: Yosys `abc` accepts an explicit `-D` delay target and uses scripts with retiming, buffering, and upsizing under constraints, ORFS exposes area-versus-speed strategy knobs, and OpenROAD’s public Ibex DOE shows that pushing the clock harder broadens the spread of outcomes while sky130hs area moved within a relatively tight 240k-280k µm² window ([Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html), [ORFS Ibex tutorial](https://openroad-flow-scripts.readthedocs.io/en/latest/tutorials/FlowTutorial.html), [OpenROAD TAU slides](https://www.tauworkshop.com/2021/speaker_slides/tom_s.pdf)).
- kind: tendency
- strength: high
- applies_to.tools: [yosys, abc, openroad]
- applies_to.pdks: [sky130hd, sky130hs, any]
- applies_to.design_class: [small_rv32_core, dsp, accelerator, bus_fabric, any]
- when: The design is already near its timing wall and the target period is being ratcheted down.
- unless: The initial target is very loose or a major architectural change resets the QoR curve.
- predicts: [(synth.area_um2_stdcell, direction, "rises slowly at first, then more noisily near the wall"), (synth.runtime_s, direction, "rises with tighter delay targets"), (synth.wns_ns, variance, "outcome spread widens when pushing harder")] ([Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html), [OpenROAD TAU slides](https://www.tauworkshop.com/2021/speaker_slides/tom_s.pdf)).
- prevents: Over-interpreting one aggressive run as the new baseline and then chasing unstable QoR; typical savings are 4-12 engineer-hours.
- rationale: Even the simple ORFS `gcd` example only shows a modest area shift between `ABC_SPEED` and `ABC_AREA`, which is a reminder that once the easy wins are gone, tighter timing often buys runtime and buffer churn more than clean architectural improvement ([ORFS Ibex tutorial](https://openroad-flow-scripts.readthedocs.io/en/latest/tutorials/FlowTutorial.html), [Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html)).
- citations: [Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html); [ORFS Ibex tutorial](https://openroad-flow-scripts.readthedocs.io/en/latest/tutorials/FlowTutorial.html); [OpenROAD TAU slides](https://www.tauworkshop.com/2021/speaker_slides/tom_s.pdf)

### R-23: abc_retiming_is_real_but_local_not_commercial_grade
- statement: Yosys+ABC already does some retiming work, because the default `abc` scripts include `dretime`, but the public open-flow gap analyses still flag register retiming and cloning as missing or insufficient for complex SoCs, so users should expect local depth smoothing rather than commercial-grade whole-chip retiming outcomes ([Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html), [OpenROAD MegaBoom issue](https://github.com/The-OpenROAD-Project/OpenROAD/issues/4623), [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)).
- kind: constraint
- strength: high
- applies_to.tools: [yosys, abc, openroad]
- applies_to.pdks: [any]
- applies_to.design_class: [accelerator, bus_fabric, any]
- when: The timing issue is local sequential balancing inside a cone.
- unless: The design needs global register movement, cloning, or multi-domain restructuring comparable to commercial signoff flows.
- predicts: [(synth.logic_depth_rel, direction, "may improve locally"), (synth.area_um2_stdcell, direction, "can rise modestly"), (synth.qor_gap_vs_commercial, likely, "persists on complex designs")] ([Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html), [OpenROAD MegaBoom issue](https://github.com/The-OpenROAD-Project/OpenROAD/issues/4623)).
- prevents: Assuming “retiming is on” means the open flow has already exhausted sequential optimization options; typical savings are 4-12 engineer-hours and better escalation decisions.
- rationale: The gap is not subtle in the public evidence: OpenROAD’s MegaBoom issue explicitly ties QoR limits to missing retiming and cloning, and the PULP 2024 talk calls out constraints and timing support as open challenges across the stack ([OpenROAD MegaBoom issue](https://github.com/The-OpenROAD-Project/OpenROAD/issues/4623), [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)).
- citations: [Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html); [OpenROAD MegaBoom issue](https://github.com/The-OpenROAD-Project/OpenROAD/issues/4623); [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)

## (G) PDK-specific quirks and extrapolation

### R-24: cross_node_area_ratios_are_useful_anchors_but_poor_extrapolators
- statement: The same Ibex RTL in maintained ORFS collateral shrinks from roughly 148k µm² synth stdcell area in sky130hd to 32.5k µm² in Nangate45 and 2.43k µm² in ASAP7, so cross-node ratios are very useful for anchor estimates, but turning those numbers into a “16nm/12nm/7nm law” is low-confidence because library architecture, routing rules, SRAM usage, and flow maturity differ dramatically across nodes ([ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json), [ORFS nangate45 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/nangate45/ibex/rules-base.json), [ORFS ASAP7 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/asap7/ibex/rules-base.json), [ASAP7 ICCAD tutorial slides](https://asap.asu.edu/wp-content/uploads/sites/47/2021/11/iccad17_asu_asap7_171115c.pdf), [Basilisk 2024](https://arxiv.org/html/2405.04257v2)).
- kind: heuristic
- strength: low
- applies_to.tools: [yosys, abc, openroad, dc, genus]
- applies_to.pdks: [sky130hd, nangate45, asap7, any]
- applies_to.design_class: [small_rv32_core, dsp, accelerator, bus_fabric, any]
- when: A program manager or architect needs a first-order node-scaling estimate before detailed implementation.
- unless: A real liberty, SRAM macro plan, and timing target already exist for the destination node.
- predicts: [(synth.area_um2_stdcell, order_of_magnitude_only, "sky130hd:nangate45:asap7 ≈ 61:13:1 in this public Ibex dataset")] ([ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json), [ORFS nangate45 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/nangate45/ibex/rules-base.json), [ORFS ASAP7 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/asap7/ibex/rules-base.json)).
- prevents: False precision in node-port business cases and misleading promises about “automatic” area collapse at advanced nodes; typical savings are 2-6 engineer-hours plus planning credibility.
- rationale: The point of the rule is to keep the useful anchor while refusing the false certainty; recent open-flow studies repeatedly emphasize how much QoR still depends on netlist structure, mux lowering, timing-awareness, and downstream physical effects rather than node label alone ([Basilisk 2024](https://arxiv.org/html/2405.04257v2), [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)).
- citations: [ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json); [ORFS nangate45 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/nangate45/ibex/rules-base.json); [ORFS ASAP7 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/asap7/ibex/rules-base.json); [ASAP7 ICCAD tutorial slides](https://asap.asu.edu/wp-content/uploads/sites/47/2021/11/iccad17_asu_asap7_171115c.pdf); [Basilisk 2024](https://arxiv.org/html/2405.04257v2); [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)

## Bibliography

- [Ibex synthesis README](https://raw.githubusercontent.com/lowRISC/ibex/master/syn/README.md)
- [Ibex register-file docs](https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html)
- [Ibex multiplier docs](https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html)
- [CARRV Ibex paper](https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf)
- [ORFS Ibex tutorial](https://openroad-flow-scripts.readthedocs.io/en/latest/tutorials/FlowTutorial.html)
- [OpenROAD Flow Variables](https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html)
- [ORFS sky130hd Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json)
- [ORFS nangate45 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/nangate45/ibex/rules-base.json)
- [ORFS ASAP7 Ibex rules](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/asap7/ibex/rules-base.json)
- [OpenROAD hierarchy discussion](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/discussions/1647)
- [OpenROAD ALU discussion](https://github.com/The-OpenROAD-Project/OpenROAD/discussions/3881)
- [OpenROAD MegaBoom issue](https://github.com/The-OpenROAD-Project/OpenROAD/issues/4623)
- [OpenROAD TAU slides](https://www.tauworkshop.com/2021/speaker_slides/tom_s.pdf)
- [Basilisk 2024](https://arxiv.org/html/2405.04257v2)
- [2024 multiplier tradeoff paper](https://arxiv.org/html/2407.03962v1)
- [PULP UCB 2024 talk](https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf)
- [Yosys flatten docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_hierarchy.html)
- [Yosys ABC command docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html)
- [Yosys opt docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_opt.html)
- [Yosys detailed opt docs](https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html)
- [Yosys dfflibmap docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.47/cmd/dfflibmap.html)
- [Yosys check docs](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_status.html)
- [Yosys proc_dlatch docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.43/cmd/proc_dlatch.html)
- [Verilator warnings guide](https://verilator.org/guide/latest/warnings.html)
- [Sky130 DFF liberty JSON](https://foss-eda-tools.googlesource.com/skywater-pdk/libs/sky130_fd_sc_hd/+/refs/heads/main/cells/dfrtp/sky130_fd_sc_hd__dfrtp_1__tt_025C_1v80.lib.json)
- [Nangate45 typical liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib)
- [ASAP7 sequential liberty](https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/asap7/lib/NLDM/asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib)
- [ASAP7 ICCAD tutorial slides](https://asap.asu.edu/wp-content/uploads/sites/47/2021/11/iccad17_asu_asap7_171115c.pdf)
