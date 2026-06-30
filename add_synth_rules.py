"""One-shot script: add researched synthesis-stage rules to the pack."""
import json
import datetime

NOW = datetime.datetime.now(datetime.timezone.utc).isoformat()

NEW_RULES = [
    # --- CDC ---
    {
        "id": "r_synth_cdc_two_ff_sync_required",
        "statement": "Every single-bit signal crossing from clock domain A to clock domain B must pass through a minimum 2-stage flip-flop synchronizer clocked by the destination domain clock.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.cdc_unsync.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.metastability_failures", "mechanism": "2-FF synchronizer resolves metastability within one clock cycle, preventing random bit flips from propagating to downstream logic."}],
        "rationale": "Unsynchronized CDC paths are the #1 cause of intermittent silicon failures. Metastability from asynchronous capture can produce arbitrary voltages that propagate as corrupted data.",
        "examples": {
            "violating": ["module bad_cdc(\n  input  clk_a, clk_b,\n  input  sig_a,\n  output reg sig_b\n);\n  // VIOLATION: direct capture without synchronizer\n  always_ff @(posedge clk_b)\n    sig_b <= sig_a;\nendmodule"],
            "compliant": ["module good_cdc(\n  input  clk_a, clk_b,\n  input  sig_a,\n  output reg sig_b\n);\n  (* ASYNC_REG = \"TRUE\" *) reg sync_1, sync_2;\n  always_ff @(posedge clk_b) begin\n    sync_1 <= sig_a;\n    sync_2 <= sync_1;\n  end\n  assign sig_b = sync_2;\nendmodule"]
        },
    },
    {
        "id": "r_synth_cdc_no_multibit_simple_sync",
        "statement": "Multi-bit signals (buses wider than 1 bit) must never cross clock domains through simple 2-FF synchronizers; use Gray-coded encoding, MCP handshake protocols, or asynchronous FIFOs.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.cdc_multibit.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.cdc_data_corruption", "mechanism": "Independent bit synchronization of multi-bit buses causes bit-skew: different bits arrive at different cycles, producing values that never existed on the source side."}],
        "rationale": "A 4-bit counter transitioning from 0111 to 1000 can be sampled as 1111 in the destination domain if bits synchronize at different edges. Gray coding ensures only 1 bit changes per transition.",
    },
    {
        "id": "r_synth_cdc_no_logic_between_sync_stages",
        "statement": "No combinational logic (gates, muxes) is permitted between the first and second stage of a synchronizer chain; stage 1 Q must connect directly to stage 2 D.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.cdc_unsync.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.metastability_propagation", "mechanism": "Combinational logic between synchronizer stages can produce glitches that propagate metastable values, defeating the synchronizer's settling time."}],
        "rationale": "The synchronizer chain relies on a full clock period for metastable resolution. Any combinational logic in the path reduces settling margin and can re-introduce glitches.",
    },
    # --- Reset ---
    {
        "id": "r_synth_async_reset_sync_deassert",
        "statement": "Asynchronous resets must be de-asserted synchronously to the destination clock domain using a reset synchronizer (2-FF chain with async reset on clear pin), preventing metastability on reset release.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.rdc.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.reset_metastability", "mechanism": "Synchronized de-assertion ensures all flops exit reset in the same clock cycle, preventing inconsistent state where some flops remain in reset one cycle longer."}],
        "rationale": "If reset de-assertion occurs near the active clock edge, some flops may capture the reset-active value while others see reset-inactive, creating an illegal state that persists until the next reset.",
    },
    {
        "id": "r_synth_no_reset_glitch_from_comb",
        "statement": "Reset signals must not be generated from combinational logic (AND/OR of multiple sources) without a registered glitch-free filter stage; use a dedicated reset controller with synchronization.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.rdc.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.spurious_reset", "mechanism": "A glitch on a combinational-derived reset line can assert reset for a fraction of a cycle, corrupting a subset of flops while leaving others unaffected."}],
        "rationale": "Combinational glitches on reset paths are undetectable by functional simulation but cause real failures in silicon when gate delays create transient pulses.",
    },
    {
        "id": "r_synth_reset_domain_crossing_sync",
        "statement": "A reset signal generated in clock domain A that controls flops in clock domain B must pass through a reset synchronizer clocked by domain B's clock.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.rdc.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.rdc_failure", "mechanism": "Reset crossing without synchronization causes metastability on reset assertion/de-assertion in the destination domain, identical to CDC metastability."}],
        "rationale": "Reset domain crossing is a special case of CDC. The same metastability physics apply to reset signals as to data signals.",
    },
    # --- Latch inference ---
    {
        "id": "r_synth_case_must_have_default",
        "statement": "In combinational blocks, every case/casex/casez statement must include a default clause that assigns all outputs, preventing unintended latch inference for unspecified case values.",
        "kind": "constraint", "strength": "high",
        "predicts": [
            {"measurement_key": "rtl.lint.case_incomplete.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"},
            {"measurement_key": "rtl.lint.latch.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}
        ],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "synth.warnings.latch", "mechanism": "Complete case coverage eliminates latch inference. Latches create timing analysis complexity, hold violations, and are not directly scannable for DFT."}],
        "rationale": "Missing default in case statements is the most common source of unintended latches in RTL. Synthesis infers storage for the unspecified conditions.",
        "examples": {
            "violating": ["always_comb begin\n  case (sel)\n    2'b00: out = a;\n    2'b01: out = b;\n    2'b10: out = c;\n    // VIOLATION: missing 2'b11 and no default\n  endcase\nend"],
            "compliant": ["always_comb begin\n  case (sel)\n    2'b00: out = a;\n    2'b01: out = b;\n    2'b10: out = c;\n    default: out = '0;  // explicit default\n  endcase\nend"]
        },
    },
    # --- Timing/Area ---
    {
        "id": "r_synth_no_combinational_loops",
        "statement": "The design must contain zero combinational feedback loops; any signal path that feeds back to itself through purely combinational logic without an intervening register is illegal and blocks synthesis.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.unoptflat.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "synth.warnings.elab_failure", "mechanism": "Synthesis tools error on combinational loops. Oscillation, indeterminate values, and timing analysis failure result from unbroken loops.", "estimated_cost_saved_hours": 20}],
        "rationale": "Combinational loops make timing analysis impossible (infinite delay paths) and cause oscillation in hardware. Verilator UNOPTFLAT detects them.",
        "examples": {
            "violating": ["// VIOLATION: a feeds back to itself through comb logic\nassign a = b & c;\nassign b = a | d;  // combinational loop: a -> b -> a"],
            "compliant": ["// Break the loop with a register\nalways_ff @(posedge clk)\n  b_reg <= a | d;\nassign a = b_reg & c;"]
        },
    },
    {
        "id": "r_synth_comb_depth_limit",
        "statement": "No combinational path between any two flip-flops shall exceed the maximum logic depth threshold (typically 20-30 levels); paths exceeding this will fail timing closure at target frequency.",
        "kind": "tendency", "strength": "medium",
        "predicts": [{"measurement_key": "rtl.module.max_comb_depth", "channel": "intervals", "value": {"min": 0, "max": 30}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "sta", "downstream_key": "sta.wns_ps", "mechanism": "Deep combinational paths have long propagation delay. If the delay exceeds the clock period minus setup time, the path fails timing.", "estimated_cost_saved_hours": 40}],
        "rationale": "Combinational depth is the strongest RTL-level predictor of timing closure difficulty. Each logic level adds ~50-100ps of delay at advanced nodes.",
    },
    {
        "id": "r_synth_fanout_limit",
        "statement": "No single non-clock/non-reset net shall drive more than the tool-configurable fanout limit (typically 16-32 loads); high-fanout nets must be flagged for buffer tree insertion.",
        "kind": "tendency", "strength": "medium",
        "predicts": [{"measurement_key": "rtl.module.max_fanout", "channel": "intervals", "value": {"min": 0, "max": 32}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "sta", "downstream_key": "sta.tns_ps", "mechanism": "Excessive fanout causes slow transitions and high capacitance, leading to transition time DRVs and setup violations across many endpoints."}],
        "rationale": "High-fanout nets without buffering create long wires and slow edges. Buffer tree insertion is cheaper when planned at RTL than fixed at P&R.",
    },
    {
        "id": "r_synth_wide_multiplier_needs_pipeline",
        "statement": "Arithmetic multipliers wider than 16 bits should use dedicated DSP blocks or pipelined implementations; single-cycle combinational multipliers wider than 32x32 bits will fail timing at most target frequencies.",
        "kind": "tendency", "strength": "medium",
        "predicts": [{"measurement_key": "rtl.operator_max_bitwidth", "channel": "intervals", "value": {"min": 0, "max": 32}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "sta", "downstream_key": "sta.wns_ps", "mechanism": "Wide multipliers create deep combinational trees (O(n^2) area, O(n) depth). Pipelining breaks the critical path."}],
        "rationale": "A 32x32 multiplier is ~2000 gates of combinational logic. Without pipelining, this is a timing closure killer at frequencies above 200MHz.",
    },
    # --- Memory inference ---
    {
        "id": "r_synth_ram_needs_sync_read",
        "statement": "For synthesis tools to infer SRAM, the read operation must be synchronous (output registered on clock edge); asynchronous reads synthesize to flip-flop arrays causing 10-100x area explosion.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "synth.memory_impl_class", "channel": "enum", "value": ["ram", "rom"], "horizon": "synth"}],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "synth.total_cell_area_um2", "mechanism": "Registered reads match SRAM library cell ports. Without registration, synthesis falls back to individual flip-flops, exploding area."}],
        "rationale": "A 1024x32 memory as flip-flops is ~32K flops. As SRAM macro, it is a single cell. The area difference can make a design unroutable.",
        "examples": {
            "violating": ["// VIOLATION: combinational read (no output register)\nalways_comb\n  rdata = mem[raddr];  // async read -> flip-flop array"],
            "compliant": ["// Synchronous read enables SRAM inference\nalways_ff @(posedge clk)\n  rdata <= mem[raddr];  // registered output -> SRAM"]
        },
    },
    {
        "id": "r_synth_large_array_needs_sram_macro",
        "statement": "Any register array larger than 256 bits total (e.g., 32x8 or 16x16) must be mapped to a compiled SRAM macro from the foundry memory compiler, not left for synthesis inference.",
        "kind": "tendency", "strength": "high",
        "predicts": [{"measurement_key": "rtl.register_bits", "channel": "intervals", "value": {"min": 0, "max": 10000}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "synth.total_cell_area_um2", "mechanism": "SRAM macros are 5-10x denser than flop arrays and consume 3-5x less power. P&R also benefits from having a rectangular hard macro vs. thousands of standard cells."}],
        "rationale": "Synthesis-inferred register files consume excessive area and power. Foundry SRAM compilers produce optimized, characterized macros with known timing.",
    },
    # --- FSM ---
    {
        "id": "r_synth_fsm_no_deadlock",
        "statement": "No FSM state shall lack an outgoing transition; every state must have at least one condition under which it transitions to another state or an explicit self-loop with terminal indication.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.unreachable_state.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.system_hang", "mechanism": "An FSM entering a deadlock state with no exit transition causes a permanent system hang requiring power cycle."}],
        "rationale": "FSM deadlocks are among the hardest bugs to debug in silicon because they often depend on rare input sequences that are hard to reproduce.",
    },
    {
        "id": "r_synth_fsm_default_safe_state",
        "statement": "The FSM case statement must include a default clause that transitions to a known safe state (typically reset/idle); for one-hot encoding, all non-one-hot bit patterns must recover to the safe state.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.fsm_no_default.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.seu_hang", "mechanism": "SEU (single-event upset) or power glitch can put FSM in an illegal state. Without a default recovery path, the FSM hangs permanently."}],
        "rationale": "In space, automotive, and safety-critical applications, soft errors can flip state register bits. The FSM must self-recover from any illegal state within one clock cycle.",
        "examples": {
            "violating": ["always_comb begin\n  case (state)\n    IDLE:  next_state = RUN;\n    RUN:   next_state = DONE;\n    DONE:  next_state = IDLE;\n    // VIOLATION: no default for illegal states\n  endcase\nend"],
            "compliant": ["always_comb begin\n  case (state)\n    IDLE:  next_state = RUN;\n    RUN:   next_state = DONE;\n    DONE:  next_state = IDLE;\n    default: next_state = IDLE;  // safe recovery\n  endcase\nend"]
        },
    },
    # --- Clock gating ---
    {
        "id": "r_synth_no_manual_clock_gate",
        "statement": "RTL must not contain manual AND/OR gates on clock signals (e.g., assign gated_clk = clk & enable); use register enable conditions and let synthesis infer ICG cells.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.gated_clock.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.clock_glitch", "mechanism": "Manual clock gating produces glitches (clock pulse truncation or spurious edges) causing setup/hold violations on all downstream flops.", "estimated_cost_saved_hours": 80}],
        "rationale": "Library ICG cells use a negative-edge latch to hold the enable, guaranteeing glitch-free gated clocks. Manual AND gates cannot provide this guarantee.",
        "examples": {
            "violating": ["// VIOLATION: manual clock gating\nassign gated_clk = clk & enable;\nalways_ff @(posedge gated_clk)\n  data_out <= data_in;"],
            "compliant": ["// Let synthesis infer ICG from enable\nalways_ff @(posedge clk)\n  if (enable)\n    data_out <= data_in;"]
        },
    },
    {
        "id": "r_synth_icg_test_bypass",
        "statement": "All clock gating cells must have a test-mode bypass (scan_enable OR'd with functional enable) so that during scan shift, clocks reach all gated flip-flops.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "dft.scan_coverage_pct", "channel": "intervals", "value": {"min": 95, "max": 100}, "horizon": "synth"}],
        "prevents": [{"downstream_stage": "dft", "downstream_key": "dft.uncontrollable_async_count", "mechanism": "Without test bypass, gated flops cannot be shifted during scan test, causing zero test coverage for those registers."}],
        "rationale": "DFT sign-off requires >95% stuck-at coverage. Ungated clock paths during scan are a prerequisite for shifting data through scan chains.",
    },
    # --- DFT ---
    {
        "id": "r_synth_dft_async_controllable",
        "statement": "All asynchronous set/reset pins on flip-flops must be controllable from a primary input or test-mode control signal during scan test; no async control may be driven solely by uncontrollable internal logic.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "dft.uncontrollable_async_count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "synth"}],
        "prevents": [{"downstream_stage": "dft", "downstream_key": "dft.scan_coverage_pct", "mechanism": "Uncontrolled async set/reset can corrupt scan chain data during shift, causing ATPG failure and reduced test coverage."}],
        "rationale": "ATPG tools need to control async resets to initialize scan chains to known states. Internal-only async controls make scan-based testing impossible for those flops.",
    },
    {
        "id": "r_synth_dft_no_tristate_contention",
        "statement": "Internal tri-state buses must have a test-mode override to prevent bus contention during scan testing; all tri-state enables must be controllable from primary inputs or test-mode signals.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "dft.uncontrollable_async_count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "synth"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.test_contention", "mechanism": "Bus contention during scan test causes indeterminate values and potential damage from short-circuit currents."}],
        "rationale": "Tri-state buses are inherently dangerous in scan mode because multiple drivers may be enabled simultaneously. Modern designs avoid internal tri-state entirely.",
    },
    # --- Pragmas ---
    {
        "id": "r_synth_no_full_case_pragma",
        "statement": "The full_case directive (// synopsys full_case or (* full_case *)) must not be used; it creates simulation/synthesis mismatch by treating unspecified cases as don't-care in synthesis but latches in simulation. Use SystemVerilog unique case instead.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.full_case_pragma.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "synth.warnings.elab_failure", "mechanism": "Pre-synthesis simulation shows latch behavior for unspecified cases, but post-synthesis hardware produces arbitrary values. Gate-level equivalence check fails.", "estimated_cost_saved_hours": 30}],
        "rationale": "Cummings (SNUG99) documented full_case and parallel_case as 'the evil twins of Verilog synthesis'. They are the most common source of simulation/synthesis mismatch.",
        "examples": {
            "violating": ["// VIOLATION: full_case creates sim/synth mismatch\nalways @(*) begin\n  // synopsys full_case\n  case (sel)\n    2'b00: y = a;\n    2'b01: y = b;\n  endcase\nend"],
            "compliant": ["// Use unique case for correct semantics\nalways_comb begin\n  unique case (sel)\n    2'b00: y = a;\n    2'b01: y = b;\n    default: y = '0;\n  endcase\nend"]
        },
    },
    {
        "id": "r_synth_no_parallel_case_pragma",
        "statement": "The parallel_case directive must not be used; it tells synthesis to implement non-priority mux logic, but simulation evaluates as priority-encoded, causing mismatch when case items overlap. Use SystemVerilog unique case instead.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.full_case_pragma.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "synth.warnings.elab_failure", "mechanism": "Priority vs. parallel logic mismatch between simulation and synthesis causes functional bugs visible only in silicon."}],
        "rationale": "parallel_case is the second 'evil twin' (Cummings SNUG99). When case items overlap (common with casex/casez), simulation picks the first match but synthesis treats all as simultaneous.",
    },
    # --- Gate-level equivalence ---
    {
        "id": "r_synth_no_blocking_in_sequential",
        "statement": "All assignments in always_ff or always @(posedge clk) blocks must use non-blocking assignments (<=); blocking assignments (=) create race conditions where simulation evaluation order determines results, but synthesis produces deterministic register hardware.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.blkseq.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "sim.race_condition_count", "mechanism": "Blocking assignments in sequential blocks allow data to 'leak' through multiple registers in one delta cycle in simulation, but hardware always takes one clock cycle per register stage."}],
        "rationale": "This is the single most important Verilog coding rule. Violation causes non-deterministic simulation that may accidentally match hardware behavior on one simulator but fail on another.",
        "examples": {
            "violating": ["always_ff @(posedge clk) begin\n  b = a;  // VIOLATION: blocking in sequential\n  c = b;  // c gets a's value this cycle (race!)\nend"],
            "compliant": ["always_ff @(posedge clk) begin\n  b <= a;  // non-blocking: correct pipeline\n  c <= b;  // c gets b's OLD value (1-cycle delay)\nend"]
        },
    },
    {
        "id": "r_synth_sensitivity_list_complete",
        "statement": "In always @(...) combinational blocks, every signal read in the block must be in the sensitivity list; use always_comb or always @(*) to auto-generate complete lists and prevent simulation/synthesis mismatch.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.latch.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "sim.x_propagation_escapes", "mechanism": "Incomplete sensitivity lists cause simulation to miss input-change events that hardware (gates) responds to immediately, creating pre-synthesis vs. post-synthesis behavioral divergence."}],
        "rationale": "always_comb is the fix: it auto-generates a complete sensitivity list and additionally checks for latch inference. There is no reason to use always @(...) in modern SV.",
    },
    {
        "id": "r_synth_no_x_assignment",
        "statement": "Synthesizable RTL must not assign X values (1'bx, 'x, '{default:'x}); synthesis treats X as don't-care and optimizes freely, while simulation propagates X as unknown, creating a fundamental semantic gap.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "sim.x_propagation_escapes", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "synth"}],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "synth.warnings.elab_failure", "mechanism": "X-assignments let synthesis pick 0 or 1 for optimization, masking bugs that X-propagation in simulation would have caught. Silicon has the bug that simulation appeared to detect."}],
        "rationale": "The X-optimism vs. X-pessimism debate is resolved by not using X in synthesizable code. Use explicit default values instead.",
        "examples": {
            "violating": ["always_comb begin\n  case (state)\n    IDLE: out = 1'b1;\n    RUN:  out = 1'b0;\n    default: out = 1'bx;  // VIOLATION: X in synth code\n  endcase\nend"],
            "compliant": ["always_comb begin\n  case (state)\n    IDLE: out = 1'b1;\n    RUN:  out = 1'b0;\n    default: out = 1'b0;  // explicit safe value\n  endcase\nend"]
        },
    },
    {
        "id": "r_synth_no_delay_in_rtl",
        "statement": "All #delay constructs must be removed from synthesizable RTL; synthesis ignores them but simulation uses them, causing timing differences between pre-synthesis and post-synthesis behavior.",
        "kind": "constraint", "strength": "medium",
        "predicts": [{"measurement_key": "rtl.lint.nonsynth.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "sim.race_condition_count", "mechanism": "Delays in RTL create false confidence in timing behavior. Gate-level simulation shows different timing than RTL simulation."}],
        "rationale": "Synthesis strips all #delays. Code that depends on delays for correctness (e.g., #1 to avoid races) masks real bugs that will appear in hardware.",
    },
    {
        "id": "r_synth_no_initial_in_asic",
        "statement": "initial blocks must not be used for state initialization in ASIC designs (they are not synthesizable); all initialization must be through reset logic. Exception: FPGA flows where initial maps to configuration bitstream.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.nonsynth.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.power_on_failure", "mechanism": "Simulation starts with initialized values from initial blocks, but ASIC silicon starts with random state. Power-on behavior will differ from simulation."}],
        "rationale": "initial blocks give false confidence: simulation always passes because state starts correct. ASIC hardware powers up to random values and will fail unpredictably.",
    },
    {
        "id": "r_synth_no_casex_for_synth",
        "statement": "casex must not be used in synthesizable code; casez should only be used with explicit don't-care patterns on the selector. Prefer SystemVerilog case...inside which has well-defined synthesis semantics.",
        "kind": "constraint", "strength": "medium",
        "predicts": [{"measurement_key": "rtl.lint.nonsynth.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "synth", "downstream_key": "sim.x_propagation_escapes", "mechanism": "casex treats both X and Z as don't-care in both selector and items, creating match conditions in simulation that differ from synthesized hardware."}],
        "rationale": "casex is the worst offender: if the selector contains X (e.g., from an uninitialized register), casex matches it as don't-care, hiding the bug. case...inside only applies don't-care to the item patterns.",
    },
    {
        "id": "r_synth_no_logic_on_clock_reset",
        "statement": "Clock and reset signals must not pass through user-defined combinational logic (AND, OR, MUX, XOR); all clock manipulation must use dedicated library cells (ICG, clock mux, clock divider) and all reset manipulation must use reset synchronizer cells.",
        "kind": "constraint", "strength": "high",
        "predicts": [{"measurement_key": "rtl.lint.gated_clock.count", "channel": "intervals", "value": {"min": 0, "max": 0}, "horizon": "rtl"}],
        "prevents": [{"downstream_stage": "silicon", "downstream_key": "silicon.clock_glitch", "mechanism": "Glitches on clock/reset from combinational logic cause spurious edge triggers, corrupting all downstream state. Extremely difficult to debug in silicon."}],
        "rationale": "Library clock cells (ICGs, muxes, dividers) are designed and characterized to be glitch-free. User RTL combinational logic has no such guarantee.",
    },
]

def main():
    with open("packs/rtl/rules.json", encoding="utf-8") as f:
        pack = json.load(f)

    existing_ids = {r["id"] for r in pack["rules"]}
    added = []
    skipped = []

    for nr in NEW_RULES:
        if nr["id"] in existing_ids:
            skipped.append(nr["id"])
            continue

        rule = {
            "id": nr["id"],
            "version": 1,
            "statement": nr["statement"],
            "kind": nr["kind"],
            "strength": nr["strength"],
            "status": "active",
            "applies_to": {
                "stage": ["rtl", "synth"],
                "tools": ["verilator", "yosys", "slang"],
                "pdks": [],
                "design_class": ["any"],
                "code_origin": ["any", "ai_generated", "ai_assisted"],
            },
            "when": [{"op": "tag", "name": "rtl_stage"}],
            "unless": [],
            "predicts": nr["predicts"],
            "prevents": nr.get("prevents", []),
            "rationale": nr.get("rationale", ""),
            "citations": [],
            "examples": nr.get("examples", {}),
            "authored_by": "research_synthesis",
            "authored_at": NOW,
            "history": [{"event": "research_added", "at": NOW,
                         "notes": "Added from synthesis-stage RTL design rules research"}],
        }
        pack["rules"].append(rule)
        existing_ids.add(nr["id"])
        added.append(nr["id"])

    # Save
    with open("packs/rtl/rules.json", "w", encoding="utf-8") as f:
        json.dump(pack, f, indent=2, ensure_ascii=False)
        f.write("\n")

    active = len([r for r in pack["rules"] if r.get("status") != "retired"])
    print(f"Added {len(added)} new synthesis rules")
    print(f"Skipped {len(skipped)} (already exist)")
    print(f"Pack now: {len(pack['rules'])} total, {active} active")
    for a in added:
        print(f"  + {a}")


if __name__ == "__main__":
    main()
