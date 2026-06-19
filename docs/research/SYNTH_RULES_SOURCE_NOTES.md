# Synth Rules Source Notes

## Key empirical numbers

- ORFS `ibex` synth stdcell area thresholds:
  - sky130hd: `148000.0` µm²
  - nangate45: `32500.0` µm²
  - asap7: `2430.0` µm²
  - Source URLs:
    - https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/sky130hd/ibex/rules-base.json
    - https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/nangate45/ibex/rules-base.json
    - https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/designs/asap7/ibex/rules-base.json

- ORFS `ibex` finish instance area thresholds:
  - sky130hd: `179425`
  - nangate45: `33440`
  - asap7: `2816`
  - Same source URLs as above.

- ORFS tutorial `ibex` sky130hd example:
  - core size about `796 x 798` µm
  - post-flow design area `191262` µm² at `30%` utilization
  - clock period `17.4 ns`
  - URL: https://openroad-flow-scripts.readthedocs.io/en/latest/tutorials/FlowTutorial.html

- CARRV 2021 Ibex config tradeoffs:
  - baseline Ibex `23.72 kGE @100 MHz`, `31.47 kGE @max freq`, max freq `500 MHz`
  - single-cycle multiplier: `+15%` area @100 MHz, `+33%` area @max freq, frequency about unchanged
  - writeback stage: `+4%` to `+5%` area, frequency about unchanged
  - SBP and BT-ALU hurt max frequency much more than SC-Mult
  - URL: https://carrv.github.io/2021/papers/CARRV2021_paper_8_Gallmann.pdf

- Current Ibex docs:
  - FF regfile is default and preferred for Verilator
  - FPGA/RAM regfile saves about `600 LUTs` and `1000 FFs` on Arty A7 versus FF regfile
  - latch regfile gives significant area savings and is first choice for ASIC
  - single-cycle multiplier uses 3 parallel 17x17 multipliers and is expected to use `3-4x` fast-mult area on ASIC
  - fast multiplier takes `3-4` cycles; slow multiplier takes `clog2(op_b)+1` cycles for MUL and `33` cycles for MULH
  - URLs:
    - https://ibex-core.readthedocs.io/en/latest/03_reference/register_file.html
    - https://ibex-core.readthedocs.io/en/latest/03_reference/instruction_decode_execute.html

- OpenROAD/Yosys hierarchy tradeoff discussion:
  - flat: maximum optimization, largest runtime
  - hierarchical: better runtime, worse results
  - practical compromise: flatten small modules, keep larger ones, often via two-pass flow
  - runtime data example for megaboom: `1_1_yosys 2768s`, `1_1_yosys_hier_report 1486s`
  - URL: https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/discussions/1647

- TAU/OpenROAD slides:
  - 22 synthesis recipes explored for Ibex
  - post-synthesis automatic PPA exploration on SKY130HS covered about `240000-280000` µm² total instance area
  - pushing clock from `9ns` to `7.2ns` improved QoR in some cases but widened outcome spread
  - URLs:
    - https://www.tauworkshop.com/2021/speaker_slides/tom_s.pdf

## Useful Yosys / ABC behavior

- `flatten` replaces cells by their implementations; `keep_hierarchy` blocks flattening.
  - URL: https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_hierarchy.html

- `keep_hierarchy -min_cost` exists to trade QoR versus runtime.
  - URL: https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_hierarchy.html

- `abc` uses liberty mapping, accepts `-D` delay target, and default scripts include `dretime`, `buffer`, `upsize`, `dnsize`, `stime -p` under constraints.
  - URL: https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html

- `abc9` upstream docs remain centered on FPGA architecture mapping; ORFS synthesis variables expose standard ABC area/speed strategies rather than ABC9 for ASIC.
  - URLs:
    - https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_techmap.html
    - https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html

- `opt` runs `opt_share` only with `-full`; `opt_clean` removes unused wires/cells; `opt_hier` propagates constants and tied signals across boundaries; `opt_balance_tree` converts cascaded add/mul/logic chains into trees to improve timing.
  - URLs:
    - https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_opt.html
    - https://yosyshq.readthedocs.io/projects/yosys/en/docs-preview-ci/cmd/index_passes_opt.html

- `dfflibmap` maps FFs to liberty cells and may add inverters; `-dont_use` can ban specific cells.
  - URL: https://yosyshq.readthedocs.io/projects/yosys/en/0.47/cmd/dfflibmap.html

- ORFS variables include area/speed strategies, hierarchical synthesis enable, keep-module list, threshold for keeping large modules, black-box clock-gating cells, and an option to remove ABC buffers.
  - URL: https://openroad-flow-scripts.readthedocs.io/en/latest/user/FlowVariables.html

## Current open-source flow limitations

- Basilisk study:
  - Yosys had limited SystemVerilog support and previously produced oversized mux trees for indexed part-selects
  - Yosys does not consider timing, fanout, or placement when reducing logic, creating very high fanout nets
  - ABC considers standard-cell timings and max fanout but not placement/routing effects
  - OpenROAD cannot fundamentally restructure the netlist in physical implementation; mostly buffers/resizes
  - URL: https://arxiv.org/html/2405.04257v2

- PULP 2024 talk:
  - Yosys has zero constraints support; ABC has very limited support
  - AIGER/BLIF interchange limits some features like multi-clock optimizations/reuse of structures
  - emap / Mockturtle work reported up to `15%` smaller average area and up to `5%` faster critical path
  - URL: https://pulp-platform.org/docs/ucb2024/BeniniUCB10-24.pdf

- OpenROAD issue on MegaBoom:
  - missing register retiming and cloning materially limits QoR versus commercial tools
  - commercial tools reached `1000 ps` on 28nm example versus open-flow `6579.57 ps`
  - URL: https://github.com/The-OpenROAD-Project/OpenROAD/issues/4623

## Warnings and review gates

- Yosys `check` identifies combinational loops, conflicting drivers, and undriven wires; `-assert` can turn these into runtime errors.
  - URL: https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/index_passes_status.html

- Yosys `proc_dlatch` identifies latches in processes and converts them to d-type latches.
  - URL: https://yosyshq.readthedocs.io/projects/yosys/en/0.43/cmd/proc_dlatch.html

- Verilator warnings:
  - `LATCH`: incomplete assignment in combinational block infers latch
  - `WIDTH` / `WIDTHTRUNC`: mismatched widths or truncation should be fixed explicitly
  - `MULTIDRIVEN`: multiple driving blocks with different clocking is bad style and can cause CDC/timing bugs
  - URL: https://verilator.org/guide/latest/warnings.html

## DFF area values used for lower-bound storage calculations

- sky130 representative DFF (`sky130_fd_sc_hd__dfrtp_1`) area: `25.024` µm²
  - URL: https://foss-eda-tools.googlesource.com/skywater-pdk/libs/sky130_fd_sc_hd/+/refs/heads/main/cells/dfrtp/sky130_fd_sc_hd__dfrtp_1__tt_025C_1v80.lib.json

- nangate45 representative DFF (`DFF_X1`) area: `4.522` µm²
  - URL: https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib

- asap7 representative DFF (`DFFHQNx1_ASAP7_75t_R`) area: `0.2916` µm²
  - URL: https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/asap7/lib/NLDM/asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib

- Lower-bound storage-only area for a 31x32 FF register file (992 bits):
  - sky130hd: `24823.8` µm²
  - nangate45: `4485.8` µm²
  - asap7: `289.3` µm²

- Lower-bound storage-only share of ORFS `ibex` synth stdcell area:
  - sky130hd: `16.8%`
  - nangate45: `13.8%`
  - asap7: `11.9%`
