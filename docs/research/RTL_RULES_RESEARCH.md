# RTL Rules Research Dossier

This dossier targets a 2026 RTL environment in which simulation-clean code is no longer enough. Vendor roadmaps now assume AI-assisted RTL authoring, lint-driven repair, and earlier visibility into timing, power, area, congestion, reset, and clock risks directly from source code rather than only after synthesis or layout ([Synopsys 2026 design workflow announcement](https://news.synopsys.com/2026-03-11-Synopsys-Outlines-Vision-for-Engineering-the-Future), [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1), [Optimizing the RTL Design Flow with Real-Time PPA Analysis](https://www.synopsys.com/blogs/chip-design/optimizing-rtl-design-flow-real-time-ppa-analysis.html)).

The rules below mix classic correctness constraints with “predictive structure” heuristics that make RTL easier for synthesis, lint, CDC/RDC, DFT, and pre-synthesis PPA estimators to reason about ([Annotating Slack Directly on Your Verilog: Fine-Grained RTL Timing Evaluation for Early Optimization](https://arxiv.org/abs/2403.18453), [Bridging Layout and RTL: Knowledge Distillation based Timing Prediction](https://proceedings.mlr.press/v267/wang25dn.html), [MasterRTL: A Pre-Synthesis PPA Estimation Framework for Any RTL Design](https://arxiv.org/abs/2311.08441), [CircuitFusion: Multimodal Hardware Circuit Representation Learning](https://arxiv.org/html/2505.02168v1)). Treat them as a practical KB for early design review: some are hard constraints, many are strong tendencies, and the shift-left PPA rules should be calibrated to your own library, synthesis recipe, and floorplan style ([PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html), [OpenROAD documentation](https://openroad.readthedocs.io/en/latest/main/README.html), [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)).

## (A) RTL coding hygiene

### R-01: comb_total_assignment
- statement: In synthesizable combinational logic, every signal written inside an `always_comb` block should be assigned on every control path, typically by setting defaults at block entry and then overriding selectively, because path-incomplete `always_comb` still infers latches and AI-generated RTL often misses exactly the closing `else` or `default` arm that prevents that outcome ([Verilator warnings](https://verilator.org/guide/latest/warnings.html), [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266)).
- kind: constraint
- strength: high
- applies_to.code_origin: [any, ai_generated, ai_assisted]
- when: when an `always_comb` or combinational decision tree writes one or more outputs, next-state variables, enables, or decoded controls.
- unless: unless an intentional latch is being modeled with `always_latch` and is explicitly reviewed as such.
- predicts: [(sim.latch_risk, risk, high), (synth.latch_count, direction, up), (sta.wns, direction, worse)]
- prevents: unintended latch inference, sim/synth mismatch, and late ECO debug; rough cost saved 4-24 engineer-hours per escaped instance.
- rationale: Modern lint stacks still flag incomplete combinational assignment as a first-order bug class because it creates storage where the reader expects pure logic ([Verilator warnings](https://verilator.org/guide/latest/warnings.html), [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)). The AI-RTL error literature and DVCon Taiwan GenAI lint workflow both reinforce that rule-driven completion of missing branches is a practical way to raise correction accuracy on generated RTL ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266), [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf)).
- citations: [Verilator warnings](https://verilator.org/guide/latest/warnings.html); [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md); [Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266)

### R-02: assignment_discipline_by_process_type
- statement: Sequential blocks should use `always_ff` with non-blocking assignments, while combinational blocks should use `always_comb` with blocking assignments, and mixing the styles should be treated as a bug unless a very specific reviewed idiom requires otherwise ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [Verilator warnings](https://verilator.org/guide/latest/warnings.html), [2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when a process models flops, latches, or pure combinational next-state/data logic.
- unless: unless the code is non-synthesizable testbench code.
- predicts: [(sim.race_risk, risk, lower), (synth.sequential_semantics_mismatch, risk, lower)]
- prevents: simulation race conditions, scheduler-order bugs, and synthesis interpretation ambiguity; rough cost saved 2-12 engineer-hours per bug.
- rationale: lowRISC explicitly codifies “sequential uses NBA, combinational uses blocking,” and Verilator keeps dedicated diagnostics for blocking-in-sequential and delayed-combinational misuse because these patterns correlate with hard-to-debug mismatches ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [Verilator warnings](https://verilator.org/guide/latest/warnings.html)). Yosys likewise recommends restricting new designs to standard always-block forms so simulators, synthesis, and formal tools interpret the RTL consistently ([2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html)).
- citations: [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md); [Verilator warnings](https://verilator.org/guide/latest/warnings.html); [2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html)

### R-03: explicit_nets_and_logic_types
- statement: Declare every signal explicitly and prefer `logic` for synthesizable RTL, using `wire` only where net semantics are actually needed, and pair that policy with `default_nettype none` or equivalent lint enforcement so typos fail fast instead of silently creating implicit one-bit nets ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when writing module ports, internal declarations, and package-visible design constants.
- unless: unless legacy Verilog-2001 import constraints force a temporary compatibility wrapper.
- predicts: [(sim.implicit_net_bug_risk, risk, lower), (rtl.signal_type_ambiguity, direction, down)]
- prevents: typo-induced floating nets, accidental 1-bit truncation, and noisy lint closure; rough cost saved 2-8 engineer-hours per escaped typo.
- rationale: The lowRISC style guide explicitly forbids inferred nets and prefers `logic` over `reg`/`wire` for synthesis-facing code, which is exactly the posture adopted by OpenTitan’s enforced lint-clean flow ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)). This rule is disproportionately valuable in AI-assisted flows because generated code often compiles syntactically yet drifts on declaration detail unless a strict lint/type barrier is present ([Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf), [Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266)).
- citations: [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md); [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html); [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf)

### R-04: width_and_signedness_are_explicit
- statement: Arithmetic widths, literal widths, and signedness should be made explicit at the point of use, especially around adds, shifts, concatenations, compares, and port hookups, because SystemVerilog otherwise permits zero-extension, sign-extension, or truncation patterns that many tools only surface as warnings ([Verilator warnings](https://verilator.org/guide/latest/warnings.html), [verible-verilog-lint](https://chipsalliance.github.io/verible/verilog_lint.html), [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when operand widths differ, unsized literals are present, signed math is intended, or the result width matters architecturally.
- unless: unless the cast, slice, or truncation is explicit and reviewed as intentional.
- predicts: [(sim.numeric_mismatch_risk, risk, lower), (synth.widthexpand_warnings, direction, down), (sim.x_optimism_risk, direction, down)]
- prevents: silent truncation, wrong sign extension, and off-by-width datapath bugs; rough cost saved 4-16 engineer-hours per arithmetic bug.
- rationale: Verilator splits width problems into truncation and expansion classes, which is a strong signal that “works in sim” is not enough for numerically sensitive RTL ([Verilator warnings](https://verilator.org/guide/latest/warnings.html)). Verible’s dedicated rules for truncated and undersized numeric literals, plus lowRISC’s requirement for explicit widths and signed constructs, make this a low-friction lint gate worth encoding as a KB rule ([verible-verilog-lint](https://chipsalliance.github.io/verible/verilog_lint.html), [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)).
- citations: [Verilator warnings](https://verilator.org/guide/latest/warnings.html); [verible-verilog-lint](https://chipsalliance.github.io/verible/verilog_lint.html); [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)

### R-05: reset_strategy_is_uniform_per_domain
- statement: Each clock/reset domain should commit to a documented reset strategy and polarity, and if asynchronous assertion is used then deassertion should be synchronized locally per clock domain instead of being allowed to float out asynchronously through the fabric ([2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html), [Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf), [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when a block contains flops with reset behavior, especially across multiple clocks or soft-reset sources.
- unless: unless the block is deliberately resetless and that choice is justified for retention, CDC, or power reasons.
- predicts: [(sta.reset_recovery_removal_risk, risk, lower), (sim.rdc_bug_risk, risk, lower), (synth.reset_style_consistency, direction, up)]
- prevents: metastable reset release, RDC escapes, and cross-module polarity bugs; rough cost saved 1-5 debug days for a subsystem issue.
- rationale: Cummings’ reset guidance is still the clearest statement of the core rule: async reset assertion can be fine, but async removal without a synchronizer is not ([Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf)). Siemens’ 2024 RDC guidance brings the modern SoC view: multiple asynchronous reset sources create their own verification problem space, and CDC checks alone do not cover it ([Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)).
- citations: [2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html); [Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf); [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)

### R-06: x_sensitive_decoding_uses_defaults_and_assertions
- statement: Decode logic should pair full output defaults with explicit illegal-condition handling and, where wildcard matching is unavoidable, assertions that trap X/Z on the selector, because `casex`/sloppy `casez` coding can make RTL simulation look deterministic while post-synthesis behavior diverges ([Yet Another Latch and Gotchas Paper](https://lcdm-eng.com/papers/snug12_Paper_final.pdf), [Verilator warnings](https://verilator.org/guide/latest/warnings.html), [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when control decode uses `case`, `casez`, `casex`, wildcard compares, or mixed output assignment across branches.
- unless: unless the selector is provably 2-state and a style waiver documents why.
- predicts: [(sim.xprop_escape_risk, risk, lower), (sim.caseincomplete_warnings, direction, down), (synth.decode_mismatch_risk, direction, down)]
- prevents: X-optimism escapes, false decoder hits after reset, and gate-level surprises; rough cost saved 1-3 debug days on control-path bugs.
- rationale: The SNUG latch gotchas paper shows exactly how X/Z in the case expression can select the wrong item under `casex` or `casez`, and why pre-case defaults are the safest latch-prevention pattern ([Yet Another Latch and Gotchas Paper](https://lcdm-eng.com/papers/snug12_Paper_final.pdf)). lowRISC’s prohibition on X-coded RTL behavior and Verilator’s case-completeness diagnostics make this rule easy to operationalize in lint and review ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- citations: [Yet Another Latch and Gotchas Paper](https://lcdm-eng.com/papers/snug12_Paper_final.pdf); [Verilator warnings](https://verilator.org/guide/latest/warnings.html); [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)

### R-07: unique_priority_used_only_for_true_intent
- statement: Use `unique`, `unique0`, or `priority` only when they match the real hardware intent, and never use old `full_case` or `parallel_case` pragmas as a shortcut, because modern SystemVerilog keywords add cross-tool checking while the old pragmas can still induce simulator-synthesis mismatches and accidental optimization of supposedly “unused” conditions ([A Solution to Verilog's "full_case" & "parallel_case" Evil Twins](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/1Sunburst%20Design/SystemVerilog's%20priority%20&%20unique%20-%20A%20Solution%20to%20Verilog's%20full_case%20&%20parallel_case%20Evil%20Twins!.pdf), [SystemVerilog Unique And Priority - How Do I Use Them?](https://www.verilogpro.com/systemverilog-unique-priority/), [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when a conditional tree claims completeness, mutual exclusivity, or ordered priority.
- unless: unless plain `case` or `if/else` better expresses the actual behavior.
- predicts: [(sim.intent_assertion_quality, direction, up), (synth.priority_logic_overbuild, direction, down), (sim.case_overlap_risk, direction, down)]
- prevents: wrong decoder semantics, optimized-away enables, and classic full_case/parallel_case escapes; rough cost saved days to weeks if it blocks a silicon-control bug.
- rationale: Cummings’ “evil twins” paper remains the canonical warning that comment pragmas can alter synthesis semantics without giving the simulator the same information ([A Solution to Verilog's "full_case" & "parallel_case" Evil Twins](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/1Sunburst%20Design/SystemVerilog's%20priority%20&%20unique%20-%20A%20Solution%20to%20Verilog's%20full_case%20&%20parallel_case%20Evil%20Twins!.pdf)). VerilogPro’s practical examples and lowRISC’s “unique case with default” convention show how to keep the benefit while avoiding blind keyword cargo-culting ([SystemVerilog Unique And Priority - How Do I Use Them?](https://www.verilogpro.com/systemverilog-unique-priority/), [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)).
- citations: [A Solution to Verilog's "full_case" & "parallel_case" Evil Twins](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/1Sunburst%20Design/SystemVerilog's%20priority%20&%20unique%20-%20A%20Solution%20to%20Verilog's%20full_case%20&%20parallel_case%20Evil%20Twins!.pdf); [SystemVerilog Unique And Priority - How Do I Use Them?](https://www.verilogpro.com/systemverilog-unique-priority/); [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)

### R-08: one_driver_per_signal_no_comb_loops
- statement: A synthesizable design signal should have one clear driver domain and should not participate in combinational feedback unless that loop is a reviewed intentional construct, because multi-driven nets, use-before-set combinational ordering, and flattened combinational loops all reduce tool predictability and DFT friendliness ([Verilator warnings](https://verilator.org/guide/latest/warnings.html), [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when a signal is written from more than one process, or a combinational block reads a value before the same block finalizes it.
- unless: unless a waiver documents a proven-safe specialized primitive wrapper.
- predicts: [(sim.comb_loop_risk, risk, lower), (synth.multidriven_warnings, direction, down), (synth.scan_blocker_risk, direction, down)]
- prevents: oscillation, unstable elaboration, poor optimization, and scan/ATPG issues; rough cost saved 4-24 engineer-hours.
- rationale: Verilator explicitly names MULTIDRIVEN, ALWCOMBORDER, and UNOPTFLAT because these constructs degrade both simulation clarity and downstream optimization quality ([Verilator warnings](https://verilator.org/guide/latest/warnings.html)). OpenTitan’s lint-clean signoff posture makes “one signal, one obvious owner” a scalable review rule rather than a taste preference ([OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)).
- citations: [Verilator warnings](https://verilator.org/guide/latest/warnings.html); [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)

## (B) FSM correctness

### R-09: fsm_encoding_is_declared_not_implied
- statement: Every nontrivial FSM should declare its intended encoding explicitly—binary, one-hot, gray, or user-defined—rather than leaving the choice ambiguous, because explicit encoding improves reviewability, makes synthesis intent testable, and is directly supported by both style guides and modern synthesis options ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [One-hot State Machine in SystemVerilog – Reverse Case Statement](https://www.verilogpro.com/systemverilog-one-hot-state-machine/), [Running Synthesis with Tcl - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/Running-Synthesis-with-Tcl)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when a state machine has more than a few states, timing significance, CDC visibility, or safety-critical behavior.
- unless: unless the FSM is trivially local and an auto-encoding decision is explicitly accepted.
- predicts: [(synth.fsm_extraction_stability, direction, up), (sta.wns, direction, more predictable), (sim.state_debug_visibility, direction, up)]
- prevents: synthesis-dependent state recoding surprises and opaque waveform/debug behavior; rough cost saved 2-12 engineer-hours.
- rationale: lowRISC requires enum-based FSM coding, while AMD exposes explicit `fsm_extraction` controls because encoding style is not a cosmetic choice—it changes the implementation ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [Running Synthesis with Tcl - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/Running-Synthesis-with-Tcl)). VerilogPro’s one-hot examples also show why making the encoding visible at the source level matters for both speed-oriented and correctness-oriented review ([One-hot State Machine in SystemVerilog – Reverse Case Statement](https://www.verilogpro.com/systemverilog-one-hot-state-machine/)).
- citations: [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md); [One-hot State Machine in SystemVerilog – Reverse Case Statement](https://www.verilogpro.com/systemverilog-one-hot-state-machine/); [Running Synthesis with Tcl - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/Running-Synthesis-with-Tcl)

### R-10: two_process_fsm_with_state_defaults
- statement: Prefer the classic two-process FSM style with a registered state block and a combinational next-state/output block whose first action is `state_d = state_q` plus full output defaults, because that structure minimizes latch risk and makes dead or missing transitions obvious in review ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [State Machine Coding Styles for Synthesis](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/02.FSM/1998-State%20Machine%20Coding%20Styles%20for%20Synthesis.pdf), [Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- kind: heuristic
- strength: high
- applies_to.code_origin: any
- when: when the FSM has explicit states and externally visible control outputs.
- unless: unless a one-block or three-block formulation is required and formally reviewed.
- predicts: [(sim.dead_state_bug_risk, direction, down), (sim.latch_risk, direction, down), (rtl.fsm_readability, direction, up)]
- prevents: missed transitions, partial assignments, and brittle future edits; rough cost saved 4-16 engineer-hours.
- rationale: Cummings’ FSM guidance and lowRISC’s style both converge on the same idea: keep the sequential state register simple and put transition logic in a block where defaults are explicit ([State Machine Coding Styles for Synthesis](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/02.FSM/1998-State%20Machine%20Coding%20Styles%20for%20Synthesis.pdf), [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)). Verilator’s latch and case warnings then become much more actionable because the structure cleanly separates state storage from decode logic ([Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- citations: [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md); [State Machine Coding Styles for Synthesis](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/02.FSM/1998-State%20Machine%20Coding%20Styles%20for%20Synthesis.pdf); [Verilator warnings](https://verilator.org/guide/latest/warnings.html)

### R-11: onehot_legality_is_checked_not_assumed
- statement: One-hot FSMs should be written so that legal-state assumptions are visible and checkable, but illegal-state recovery should not be trusted to a casual default arm under `unique case`, because synthesis can optimize around those assumptions and make “recover to IDLE” behavior less real than it looks in RTL ([One-hot State Machine in SystemVerilog – Reverse Case Statement](https://www.verilogpro.com/systemverilog-one-hot-state-machine/), [A Solution to Verilog's "full_case" & "parallel_case" Evil Twins](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/1Sunburst%20Design/SystemVerilog's%20priority%20&%20unique%20-%20A%20Solution%20to%20Verilog's%20full_case%20&%20parallel_case%20Evil%20Twins!.pdf), [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)).
- kind: heuristic
- strength: medium
- applies_to.code_origin: any
- when: when one-hot encoding, reverse-case style, or index-based enums are used.
- unless: unless a safe-state mechanism is formally proven and synthesized with matching constraints.
- predicts: [(sim.onehot_violation_visibility, direction, up), (synth.illegal_state_recovery_reliability, direction, more predictable)]
- prevents: parasitic exit logic, misleading recovery defaults, and control divergence after single-event upset; rough cost saved 1-3 debug days in safety review.
- rationale: VerilogPro explicitly warns that `unique case` can override intuitive “default to IDLE” recovery expectations in one-hot code ([One-hot State Machine in SystemVerilog – Reverse Case Statement](https://www.verilogpro.com/systemverilog-one-hot-state-machine/)). Cummings’ unique/priority guidance explains why: the keyword communicates strong assumptions to synthesis, not just to the simulator ([A Solution to Verilog's "full_case" & "parallel_case" Evil Twins](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/1Sunburst%20Design/SystemVerilog's%20priority%20&%20unique%20-%20A%20Solution%20to%20Verilog's%20full_case%20&%20parallel_case%20Evil%20Twins!.pdf)).
- citations: [One-hot State Machine in SystemVerilog – Reverse Case Statement](https://www.verilogpro.com/systemverilog-one-hot-state-machine/); [A Solution to Verilog's "full_case" & "parallel_case" Evil Twins](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/1Sunburst%20Design/SystemVerilog's%20priority%20&%20unique%20-%20A%20Solution%20to%20Verilog's%20full_case%20&%20parallel_case%20Evil%20Twins!.pdf); [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)

### R-12: fsm_has_known_reset_and_no_unreachable_sink_states
- statement: An FSM should reset into exactly one known legal state and should be checked for unreachable, terminal, and unhandled encodings, because “default catches it” is not the same thing as proving the state graph is complete and sane ([State Machine Coding Styles for Synthesis](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/02.FSM/1998-State%20Machine%20Coding%20Styles%20for%20Synthesis.pdf), [Verilator warnings](https://verilator.org/guide/latest/warnings.html), [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when the state space exceeds a trivial handshake controller.
- unless: unless the FSM is transient/generated and is already covered by formal reachability checks.
- predicts: [(sim.dead_state_bug_risk, risk, lower), (sim.caseincomplete_warnings, direction, down), (sta.control_path_debug_time, direction, down)]
- prevents: stuck controllers, illegal power-up behavior, and incomplete decode hazards; rough cost saved 4-24 engineer-hours.
- rationale: The older SNUG FSM literature is still useful here because it stresses symbolic state definition, explicit reset state, and complete next-state logic as synthesis-facing concerns, not just style ([State Machine Coding Styles for Synthesis](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/02.FSM/1998-State%20Machine%20Coding%20Styles%20for%20Synthesis.pdf)). In a modern flow, lint-clean continuous integration makes dead or unhandled state encodings cheap to catch before they become waveform archaeology ([OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html), [Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- citations: [State Machine Coding Styles for Synthesis](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/02.FSM/1998-State%20Machine%20Coding%20Styles%20for%20Synthesis.pdf); [Verilator warnings](https://verilator.org/guide/latest/warnings.html); [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)

## (C) Clock-domain crossing (CDC)

### R-13: single_bit_cdc_uses_registered_source_and_2ff_sync
- statement: A single-bit CDC control should be registered in the source domain and synchronized in the destination domain with a 2-FF synchronizer, or 3-FF when MTBF pressure justifies it, instead of feeding raw combinational control across the boundary ([Clock Domain Crossing (CDC) Design & Verification Techniques Using SystemVerilog](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/04.SystemVerilog/2008-Clock%20Domain%20Crossing%20(CDC)%20Design%20&%20Verification%20Techniques%20Using%20SystemVerilog.pdf), [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when a single pulse, flag, interrupt, toggle, or valid bit crosses between asynchronous or mesochronous clocks.
- unless: unless a hardened synchronizer cell or proven protocol wrapper is used.
- predicts: [(sta.metastability_risk, direction, down), (sim.cdc_escape_risk, direction, down)]
- prevents: metastability propagation and pulse loss at the boundary; rough cost saved days to weeks if it avoids a lab-only intermittent failure.
- rationale: Cummings’ CDC paper is still the practical baseline: source-register first, then 2-FF synchronize, and widen to 3-FF only when MTBF math says 2-FF is not enough ([Clock Domain Crossing (CDC) Design & Verification Techniques Using SystemVerilog](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/04.SystemVerilog/2008-Clock%20Domain%20Crossing%20(CDC)%20Design%20&%20Verification%20Techniques%20Using%20SystemVerilog.pdf)). Siemens’ RDC note is about resets, but it reinforces the broader point that asynchronous boundaries create fault modes that ordinary RTL simulation under-covers ([Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)).
- citations: [Clock Domain Crossing (CDC) Design & Verification Techniques Using SystemVerilog](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/04.SystemVerilog/2008-Clock%20Domain%20Crossing%20(CDC)%20Design%20&%20Verification%20Techniques%20Using%20SystemVerilog.pdf); [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)

### R-14: multibit_cdc_uses_protocol_not_bitwise_sync
- statement: Multi-bit data or correlated multi-control CDC should use a handshake, toggle-plus-data multi-cycle-path formulation, Gray coding, or an async FIFO, and should never be synchronized bit-by-bit as if each wire were an independent single-bit flag ([Clock Domain Crossing (CDC) Design & Verification Techniques Using SystemVerilog](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/04.SystemVerilog/2008-Clock%20Domain%20Crossing%20(CDC)%20Design%20&%20Verification%20Techniques%20Using%20SystemVerilog.pdf), [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when more than one bit must be sampled coherently at the destination, including counters, pointers, encoded state, enable+data bundles, or load+valid pairs.
- unless: unless the bus is statically held stable for a verified capture window under a proven protocol.
- predicts: [(sim.cdc_bus_skew_risk, risk, lower), (sim.data_corruption_risk, direction, down)]
- prevents: skewed bus capture, false full/empty conditions, and reconvergence bugs; rough cost saved 1-5 debug days.
- rationale: Cummings gives concrete failure cases for independently synchronized multi-bit controls and then walks through the safe alternatives: consolidation, MCP, Gray code, and FIFO-based transfer ([Clock Domain Crossing (CDC) Design & Verification Techniques Using SystemVerilog](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/04.SystemVerilog/2008-Clock%20Domain%20Crossing%20(CDC)%20Design%20&%20Verification%20Techniques%20Using%20SystemVerilog.pdf)). The shift-left survey is useful because it frames early structural risk prediction as part of modern RTL practice, which means CDC protocol choice belongs in the RTL KB rather than in a back-end-only checklist ([Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)).
- citations: [Clock Domain Crossing (CDC) Design & Verification Techniques Using SystemVerilog](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/04.SystemVerilog/2008-Clock%20Domain%20Crossing%20(CDC)%20Design%20&%20Verification%20Techniques%20Using%20SystemVerilog.pdf); [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)

### R-15: rdc_is_a_first_class_design_rule
- statement: Reset domain crossing should be treated with the same seriousness as CDC, meaning asynchronous reset release is synchronized per receiving clock domain and paths between unlike reset domains are reviewed with dedicated RDC methodology, not waived as “same clock so probably fine” ([Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf), [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/), [DFT architectural tips: testing of asynchronous sets/resets](https://blogs.sw.siemens.com/tessent/2019/07/24/dft-architectural-tips-testing-of-asynchronous-sets-resets/)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when soft resets, power-managed resets, block-local resets, or mixed-reset subtrees exist.
- unless: unless the entire path is within one proven-correlated reset domain.
- predicts: [(sim.rdc_bug_risk, direction, down), (sta.reset_recovery_removal_risk, direction, down), (synth.async_reset_test_controllability, direction, up)]
- prevents: reset glitches, metastable release, and test-mode corruption; rough cost saved 1-5 debug days plus ATPG cleanup.
- rationale: Cummings makes the core electrical point—async reset removal is itself an asynchronous event that needs synchronization ([Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf)). Siemens extends that to modern SoCs by arguing that RDC is not a CDC footnote but its own signoff discipline, while Tessent shows that uncontrolled async resets also directly degrade testability ([Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/), [DFT architectural tips: testing of asynchronous sets/resets](https://blogs.sw.siemens.com/tessent/2019/07/24/dft-architectural-tips-testing-of-asynchronous-sets-resets/)).
- citations: [Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf); [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/); [DFT architectural tips: testing of asynchronous sets/resets](https://blogs.sw.siemens.com/tessent/2019/07/24/dft-architectural-tips-testing-of-asynchronous-sets-resets/)

### R-16: avoid_ad_hoc_derived_or_gated_clocks
- statement: Avoid hand-rolled derived or gated clocks in RTL for ordinary control purposes and prefer clock-enable semantics, because direct logic in the clock path creates glitch, skew, and analysis problems that synthesis tools often try to convert back into enables anyway ([Converting Clock Gating to Clock Enable - 2025.1 English - AMD](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Converting-Clock-Gating-to-Clock-Enable), [Clock Gating | Home](https://24x7fpga.com/rtl_directory/2024_09_13_12_36_11_clock_gating/), [Implementing automatic clock gating in the OpenROAD ASIC design toolchain](https://antmicro.com/blog/2025/07/automatic-clock-gating-in-openroad/)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when an enable, pause, or block-idle condition is being translated into a new clock-like signal.
- unless: unless a library ICG cell or architecturally required generated clock is used with proper constraints.
- predicts: [(sta.clock_skew_risk, direction, down), (sta.hold_violation_risk, direction, down), (sim.gated_clock_glitch_risk, direction, down)]
- prevents: glitchy clocks, skew-driven hold failures, and CDC/constraint noise; rough cost saved 1-3 implementation iterations.
- rationale: AMD’s guidance is blunt: convert clock gating logic to clock enables when possible because it maps better to clock resources and simplifies timing analysis ([Converting Clock Gating to Clock Enable - 2025.1 English - AMD](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Converting-Clock-Gating-to-Clock-Enable)). Recent clock-gating writeups from both 24x7FPGA and Antmicro show why ad-hoc logic is brittle and why modern flows prefer either proper ICG insertion or mathematically checked automatic gating ([Clock Gating | Home](https://24x7fpga.com/rtl_directory/2024_09_13_12_36_11_clock_gating/), [Implementing automatic clock gating in the OpenROAD ASIC design toolchain](https://antmicro.com/blog/2025/07/automatic-clock-gating-in-openroad/)).
- citations: [Converting Clock Gating to Clock Enable - 2025.1 English - AMD](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Converting-Clock-Gating-to-Clock-Enable); [Clock Gating | Home](https://24x7fpga.com/rtl_directory/2024_09_13_12_36_11_clock_gating/); [Implementing automatic clock gating in the OpenROAD ASIC design toolchain](https://antmicro.com/blog/2025/07/automatic-clock-gating-in-openroad/)

## (D) Synthesizability

### R-17: dut_rtl_stays_within_synthesizable_subset
- statement: Design RTL should stay inside the standard synthesizable subset and exclude behavioral conveniences such as timing delays in RTL processes, `force/release`, or `fork...join`-style concurrency that belongs in verification code, because open and commercial tools still differ most sharply at those edges ([2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html), [Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when code is intended to become gates rather than testbench stimulus.
- unless: unless the construct is inside a simulation-only region or wrapper excluded from synthesis.
- predicts: [(synth.elaboration_failure_risk, risk, lower), (sim_tool_divergence_risk, direction, down)]
- prevents: synthesis rejection, silent semantic change, and tool portability churn; rough cost saved 2-16 engineer-hours.
- rationale: Yosys recommends limiting new designs to the standardized always forms because those are the cases all synthesis tools agree on ([2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html)). Verilator’s warnings around delayed procedural constructs and forked lifetimes are a reminder that even “just for convenience” syntax can push code out of the safe portable subset ([Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- citations: [2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html); [Verilator warnings](https://verilator.org/guide/latest/warnings.html)

### R-18: no_hierarchical_refs_in_synth_rtl
- statement: Synthesizable RTL should not read or write internal signals of another module through hierarchical references, and cross-module connectivity should be expressed through ports, interfaces, or package-visible constants instead ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when one module appears to “reach into” another module’s internals or parameters.
- unless: unless the reference is in non-synthesizable assertions/debug code and is tool-guarded.
- predicts: [(synth.hierarchy_preservation_risk, direction, lower), (synth.elaboration_portability, direction, up)]
- prevents: broken elaboration, hierarchy-coupled ECO pain, and portability failures across tools; rough cost saved 4-16 engineer-hours.
- rationale: lowRISC flatly prohibits hierarchical references in synthesizable RTL because they undermine modularity and tool compatibility ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)). Verilator’s HIERPARAM error shows the same issue from the elaboration side: these shortcuts quickly become impossible to support consistently ([Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- citations: [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md); [Verilator warnings](https://verilator.org/guide/latest/warnings.html)

### R-19: configuration_uses_parameters_not_preprocessor_state
- statement: Module configurability should use typed `parameter`/`localparam` or package constants rather than design-shaping `define` macros or `defparam`, because elaboration-time parameters are visible to tools and review while preprocessor state is global, textual, and easier to misuse ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [Understanding `define, parameter, and localparam in SystemVerilog](https://vlsiworlds.com/system-verilog/understanding-define-parameter-and-localparam-in-systemverilog/)).
- kind: constraint
- strength: medium
- applies_to.code_origin: [any, legacy_imported]
- when: when width, depth, feature knobs, or state counts vary by instantiation or build target.
- unless: unless the macro is only an include guard or a simulation-only compile switch.
- predicts: [(synth.config_traceability, direction, up), (rtl.build_variant_bug_risk, direction, down)]
- prevents: hidden global build dependencies and hard-to-reproduce configuration bugs; rough cost saved 2-12 engineer-hours.
- rationale: lowRISC’s style guide is explicit that `define` should never be used to parameterize a module and that typed parameters are preferred for both safety and readability ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)). Even mainstream explanatory material from 2025 still has to emphasize the same point, which suggests the failure mode remains common in working codebases ([Understanding `define, parameter, and localparam in SystemVerilog](https://vlsiworlds.com/system-verilog/understanding-define-parameter-and-localparam-in-systemverilog/)).
- citations: [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md); [Understanding `define, parameter, and localparam in SystemVerilog](https://vlsiworlds.com/system-verilog/understanding-define-parameter-and-localparam-in-systemverilog/)

## (E) Shift-left PPA prediction at RTL

### R-20: reg_to_reg_comb_depth_is_a_first_order_timing_signal
- statement: Long register-to-register combinational depth at RTL should be treated as an early timing debt signal, because the best recent RTL timing predictors derive their value precisely from structural cues around path depth, arrival time, and critical-path shape, and deeper stages tend to correlate with worse downstream WNS/TNS unless later pipelined or rebalanced ([Annotating Slack Directly on Your Verilog: Fine-Grained RTL Timing Evaluation for Early Optimization](https://arxiv.org/abs/2403.18453), [Bridging Layout and RTL: Knowledge Distillation based Timing Prediction](https://proceedings.mlr.press/v267/wang25dn.html), [MasterRTL: A Pre-Synthesis PPA Estimation Framework for Any RTL Design](https://arxiv.org/abs/2311.08441), [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)).
- kind: tendency
- strength: high
- applies_to.code_origin: any
- when: when a stage contains wide arithmetic, deep mux trees, long compare/decode chains, or fanout-heavy control before the next flop.
- unless: unless synthesis retiming or dedicated hard macros are intentionally expected to absorb that depth.
- predicts: [(rtl.max_comb_levels_per_stage, channel, up), (sta.wns, direction, worse), (sta.tns, direction, worse)]
- prevents: late discovery of negative slack and reactive pipeline ECOs; rough cost saved 1-3 implementation turns.
- rationale: RTL-Timer is directly about register-endpoint arrival-time prediction and reported correlation above 0.89 plus measurable post-optimization WNS/TNS gains, which is strong evidence that structural timing signals are already present in RTL ([Annotating Slack Directly on Your Verilog: Fine-Grained RTL Timing Evaluation for Early Optimization](https://arxiv.org/abs/2403.18453)). RTLDistil, MasterRTL, and the 2025 shift-left survey all reinforce the same point from different angles: timing is now a predictable RTL-stage property, not purely a post-layout surprise ([Bridging Layout and RTL: Knowledge Distillation based Timing Prediction](https://proceedings.mlr.press/v267/wang25dn.html), [MasterRTL: A Pre-Synthesis PPA Estimation Framework for Any RTL Design](https://arxiv.org/abs/2311.08441), [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)).
- citations: [Annotating Slack Directly on Your Verilog: Fine-Grained RTL Timing Evaluation for Early Optimization](https://arxiv.org/abs/2403.18453); [Bridging Layout and RTL: Knowledge Distillation based Timing Prediction](https://proceedings.mlr.press/v267/wang25dn.html); [MasterRTL: A Pre-Synthesis PPA Estimation Framework for Any RTL Design](https://arxiv.org/abs/2311.08441); [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)

### R-21: rtl_fanout_and_hot_hierarchy_predict_congestion
- statement: Wide-spread control nets, giant mux cones, and modules with disproportionate fan-in/fan-out should be flagged at RTL as congestion candidates, because both commercial physically aware tools and open flows surface congestion, max-fanout, and hierarchy-level hotspots as meaningful pre-route indicators ([Optimizing the RTL Design Flow with Real-Time PPA Analysis](https://www.synopsys.com/blogs/chip-design/optimizing-rtl-design-flow-real-time-ppa-analysis.html), [Addressing Congestion - 2025.1 English - UG949](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Addressing-Congestion), [OpenROAD documentation](https://openroad.readthedocs.io/en/latest/main/README.html), [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)).
- kind: tendency
- strength: medium
- applies_to.code_origin: any
- when: when one module owns many global controls, broadcasts, or exceptionally wide decode trees that touch distant logic.
- unless: unless floorplan locality and buffering strategy are already designed around that topology.
- predicts: [(synth.max_fanout, direction, up), (synth.route_congestion_hotspot_risk, direction, up), (sta.hold_fix_detours, direction, up)]
- prevents: congestion-driven detours, buffering explosion, and noisy hold closure; rough cost saved 1-2 PnR iterations.
- rationale: RTL Architect explicitly aggregates timing, power, and congestion by hierarchy and even by source construct/line, which is effectively a public endorsement that RTL structure predicts physical stress points ([Optimizing the RTL Design Flow with Real-Time PPA Analysis](https://www.synopsys.com/blogs/chip-design/optimizing-rtl-design-flow-real-time-ppa-analysis.html)). AMD and OpenROAD document the same downstream symptoms through different interfaces—congested regions, top modules in the window, and max-fanout/timing metrics—which makes this a sound shift-left heuristic even before adopting ML predictors ([Addressing Congestion - 2025.1 English - UG949](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Addressing-Congestion), [OpenROAD documentation](https://openroad.readthedocs.io/en/latest/main/README.html)).
- citations: [Optimizing the RTL Design Flow with Real-Time PPA Analysis](https://www.synopsys.com/blogs/chip-design/optimizing-rtl-design-flow-real-time-ppa-analysis.html); [Addressing Congestion - 2025.1 English - UG949](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Addressing-Congestion); [OpenROAD documentation](https://openroad.readthedocs.io/en/latest/main/README.html); [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)

### R-22: register_bits_are_a_first_order_ff_count_proxy
- statement: As a first-order planning heuristic, tracked RTL register bits should be treated as a near-linear proxy for post-synthesis flip-flop count, with the important caveat that retiming, shift-register extraction, RAM inference, and register merging can change the exact multiplier ([PRIMAL: Power Inference using Machine Learning](https://research.nvidia.com/sites/default/files/pubs/2019-06_PRIMAL:-Power-Inference/24_1_Zhou_PRIMAL.pdf), [CircuitFusion: Multimodal Hardware Circuit Representation Learning](https://arxiv.org/html/2505.02168v1), [Running Synthesis with Tcl - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/Running-Synthesis-with-Tcl)).
- kind: identity
- strength: medium
- applies_to.code_origin: any
- when: when doing early area/power budgeting or comparing two RTL architectures with materially different state footprints.
- unless: unless the state is likely to map into memories, SRLs, or be aggressively retimed/merged.
- predicts: [(rtl.register_bits, ratio_to, synth.ff_count ~ 0.9x-1.1x before structural transforms), (synth.area, direction, up), (sta.clock_power, direction, up)]
- prevents: under-budgeting state cost and missing the power/area impact of “just a few more registers”; rough cost saved 4-12 planning hours per architectural comparison.
- rationale: PRIMAL states a one-to-one correspondence between RTL and gate-level registers as the basis for learning power from RTL traces, which is precisely the empirical justification for using register-bit count as a first-order resource metric ([PRIMAL: Power Inference using Machine Learning](https://research.nvidia.com/sites/default/files/pubs/2019-06_PRIMAL:-Power-Inference/24_1_Zhou_PRIMAL.pdf)). CircuitFusion and synthesis options such as retiming, SRL extraction, and equivalent-register merging explain why the rule should stay a heuristic rather than a hard law ([CircuitFusion: Multimodal Hardware Circuit Representation Learning](https://arxiv.org/html/2505.02168v1), [Running Synthesis with Tcl - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/Running-Synthesis-with-Tcl)).
- citations: [PRIMAL: Power Inference using Machine Learning](https://research.nvidia.com/sites/default/files/pubs/2019-06_PRIMAL:-Power-Inference/24_1_Zhou_PRIMAL.pdf); [CircuitFusion: Multimodal Hardware Circuit Representation Learning](https://arxiv.org/html/2505.02168v1); [Running Synthesis with Tcl - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/Running-Synthesis-with-Tcl)

### R-23: memory_templates_are_written_for_intended_inference
- statement: Arrays that are logically memories should be coded in inference-friendly templates whose read/write semantics match the desired implementation, because read synchronicity, port pattern, and size strongly steer whether the structure lands in FF RAM, LUT/distributed RAM, or block RAM and therefore dominate area and timing tradeoffs ([Memory handling — YosysHQ Yosys 0.43 documentation](https://yosyshq.readthedocs.io/projects/yosys/en/0.43/using_yosys/synthesis/memory.html), [RAM_STYLE - 2024.1 English - UG912](https://docs.amd.com/r/2024.1-English/ug912-vivado-properties/RAM_STYLE), [Choosing Between Distributed RAM and Dedicated Block RAM - 2024.2 English](https://docs.amd.com/r/2024.2-English/ug901-vivado-synthesis/Choosing-Between-Distributed-RAM-and-Dedicated-Block-RAM?contentId=P5lJu6Y5~LTu~iWW~10fBQ)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when coding regfiles, FIFOs, scoreboards, ROMs, line buffers, or shift-register-like storage.
- unless: unless the implementation is intentionally forced with explicit vendor or library instantiation.
- predicts: [(synth.memory_impl_class, channel, FF_RAM_or_LUTRAM_or_BRAM), (synth.area, direction, large swing), (sta.wns, direction, implementation-dependent)]
- prevents: accidental register-based memories, missed BRAM inference, and area blowups; rough cost saved 1-3 synthesis iterations.
- rationale: Yosys is unusually clear that asynchronous reads force LUT/FF-style memory while synchronous-read, supported-port templates can become block RAM, which makes RTL memory semantics an implementation decision in disguise ([Memory handling — YosysHQ Yosys 0.43 documentation](https://yosyshq.readthedocs.io/projects/yosys/en/0.43/using_yosys/synthesis/memory.html)). AMD documents the same behavior from the FPGA side through RAM_STYLE and the sync-vs-async distinction between dedicated block RAM and distributed RAM ([RAM_STYLE - 2024.1 English - UG912](https://docs.amd.com/r/2024.1-English/ug912-vivado-properties/RAM_STYLE), [Choosing Between Distributed RAM and Dedicated Block RAM - 2024.2 English](https://docs.amd.com/r/2024.2-English/ug901-vivado-synthesis/Choosing-Between-Distributed-RAM-and-Dedicated-Block-RAM?contentId=P5lJu6Y5~LTu~iWW~10fBQ)).
- citations: [Memory handling — YosysHQ Yosys 0.43 documentation](https://yosyshq.readthedocs.io/projects/yosys/en/0.43/using_yosys/synthesis/memory.html); [RAM_STYLE - 2024.1 English - UG912](https://docs.amd.com/r/2024.1-English/ug912-vivado-properties/RAM_STYLE); [Choosing Between Distributed RAM and Dedicated Block RAM - 2024.2 English](https://docs.amd.com/r/2024.2-English/ug901-vivado-synthesis/Choosing-Between-Distributed-RAM-and-Dedicated-Block-RAM?contentId=P5lJu6Y5~LTu~iWW~10fBQ)

### R-24: operator_width_and_pipeline_depth_drive_datapath_cost
- statement: Wide arithmetic operators should be budgeted together with target throughput and pipeline depth, because synthesis may infer DSPs or large logic structures based on width and timing pressure, and real cores like Ibex visibly trade area for faster multipliers and extra pipeline staging ([USE_DSP - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/USE_DSP), [ibex/README.md at master · lowRISC/ibex](https://github.com/lowRISC/ibex/blob/master/README.md), [Pipeline Details - Ibex](https://ibex-core.readthedocs.io/en/latest/03_reference/pipeline_details.html)).
- kind: tendency
- strength: medium
- applies_to.code_origin: any
- when: when adding multipliers, MACs, dividers, wide adders, or throughput-driven datapath replication.
- unless: unless the operator maps to a fixed hardened macro with known latency/area.
- predicts: [(rtl.operator_bitwidth, direction, up), (synth.dsp_or_logic_usage, direction, up), (synth.area, direction, up), (sta.wns, direction, worse unless pipelined)]
- prevents: accidentally oversized datapaths and under-pipelined multiply stages; rough cost saved 4-16 architecture exploration hours.
- rationale: AMD explicitly says multipliers and multiply-accumulate forms are DSP-inference candidates subject to timing concerns and thresholds, which means width and performance target both matter to the final implementation ([USE_DSP - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/USE_DSP)). Ibex gives a concrete public example: the 3-cycle multiplier and 1-cycle multiplier configurations move both performance and area, and the writeback stage further changes the performance envelope ([ibex/README.md at master · lowRISC/ibex](https://github.com/lowRISC/ibex/blob/master/README.md), [Pipeline Details - Ibex](https://ibex-core.readthedocs.io/en/latest/03_reference/pipeline_details.html)).
- citations: [USE_DSP - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/USE_DSP); [ibex/README.md at master · lowRISC/ibex](https://github.com/lowRISC/ibex/blob/master/README.md); [Pipeline Details - Ibex](https://ibex-core.readthedocs.io/en/latest/03_reference/pipeline_details.html)

### R-25: expose_clock_gating_opportunities_as_clean_enables
- statement: Idle behavior should be expressed as stable register enables or shared low-activity guard conditions in the functional RTL, because modern power-estimation and auto-gating flows look for exactly those patterns when estimating clock/data-path/glitch power and when proving that a gating condition is safe to insert ([PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html), [Implementing automatic clock gating in the OpenROAD ASIC design toolchain](https://antmicro.com/blog/2025/07/automatic-clock-gating-in-openroad/), [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)).
- kind: heuristic
- strength: high
- applies_to.code_origin: any
- when: when a register bank, counter, FSM, or datapath only updates on intermittent valid/enable conditions.
- unless: unless manual clock architecture or protocol semantics prevent gating.
- predicts: [(rtl.clock_gating_candidates, direction, up), (synth.icg_insertion_opportunity, direction, up), (synth.power, direction, down)]
- prevents: leaving easy dynamic-power savings undiscoverable until late optimization; rough cost saved one power-optimization spin and associated analysis time.
- rationale: PrimePower RTL explicitly advertises clock-gating, memory, datapath, and glitch-power exploration from RTL, which means enable visibility is already part of the power model surface ([PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html)). Antmicro’s OpenROAD gating work makes the structural requirement concrete: candidate nets must be related to the registers, low-activity, and SAT-checkable as a correct gating condition ([Implementing automatic clock gating in the OpenROAD ASIC design toolchain](https://antmicro.com/blog/2025/07/automatic-clock-gating-in-openroad/)).
- citations: [PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html); [Implementing automatic clock gating in the OpenROAD ASIC design toolchain](https://antmicro.com/blog/2025/07/automatic-clock-gating-in-openroad/); [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)

## (F) AI-generated-RTL specific hazards

### R-26: ai_comb_blocks_need_missing_arm_checks
- statement: For AI-generated or AI-assisted combinational code, missing `else`/`default` arms should be assumed to be a common failure mode and checked automatically before review proceeds, because current error analyses point to domain-knowledge gaps and lint-fix workflows still show strong dependence on rule-specific correction passes ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266), [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf), [Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- kind: heuristic
- strength: high
- applies_to.code_origin: [ai_generated, ai_assisted]
- when: when the code was drafted from prompts, autocomplete, or model-transformed lint repair.
- unless: unless a structural lint gate has already proven total assignment on every combinational LHS.
- predicts: [(sim.latch_risk, risk, high), (synth.latch_count, direction, up)]
- prevents: the most common “looks plausible but stores state” failure class in generated RTL; rough cost saved 2-12 engineer-hours per block.
- rationale: The 2025 LLM-RTL error paper says the dominant issue is not abstract reasoning but missing RTL programming knowledge, which makes omission-style bugs unsurprising ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266)). DVCon Taiwan’s results then show why segmentation by rule matters: AI correction quality varies sharply by violation type, so latch-class checks should stay explicit and local ([Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf)).
- citations: [Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266); [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf); [Verilator warnings](https://verilator.org/guide/latest/warnings.html)

### R-27: ai_seq_blocks_get_assignment_type_audit
- statement: AI-origin sequential logic should receive a dedicated audit for blocking versus non-blocking assignment misuse, because assignment-style drift is a known RTL-knowledge failure mode and lint correction accuracy depends heavily on the exact rule being targeted ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266), [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf), [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- kind: heuristic
- strength: high
- applies_to.code_origin: [ai_generated, ai_assisted]
- when: when a model has emitted or modified `always_ff`, `always @(posedge ...)`, or mixed sequential/combinational logic.
- unless: unless lint has already proven zero BLKSEQ-style issues.
- predicts: [(sim.race_risk, direction, up), (synth.sequential_semantics_mismatch, direction, up)]
- prevents: scheduler-order bugs and accidental simulation-only behavior; rough cost saved 2-8 engineer-hours.
- rationale: The AI error analysis points to insufficient specialized RTL knowledge as the core bottleneck, and assignment semantics are exactly the kind of domain-specific habit that generic code models miss ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266)). lowRISC and Verilator make the review target objective by encoding the correct discipline into lintable rules ([lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md), [Verilator warnings](https://verilator.org/guide/latest/warnings.html)).
- citations: [Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266); [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf); [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md); [Verilator warnings](https://verilator.org/guide/latest/warnings.html)

### R-28: ai_reset_polarity_and_release_are_cross_module_checked
- statement: AI-origin modules should be checked in groups for reset polarity, naming, assertion style, and deassert synchronization, because generated code often looks locally consistent while disagreeing at the subsystem level about whether reset is active-high, active-low, synchronous, or asynchronous ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266), [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf), [Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf), [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)).
- kind: heuristic
- strength: high
- applies_to.code_origin: [ai_generated, ai_assisted]
- when: when multiple model-authored modules participate in the same reset tree or subsystem.
- unless: unless a package/interface centralizes reset semantics and the generator was constrained to it.
- predicts: [(sim.rdc_bug_risk, direction, up), (synth.reset_style_consistency, direction, down)]
- prevents: polarity mismatches, reset deadlocks, and hidden RDC bugs; rough cost saved 1-4 debug days.
- rationale: The 2025 error analysis says design-description ambiguity and missing circuit knowledge remain major failure causes, which is exactly how reset conventions drift between generated modules ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266)). Classic reset guidance plus modern RDC methodology makes this an easy “cross-file semantic lint” rule rather than a subjective code-review concern ([Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf), [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)).
- citations: [Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266); [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf); [Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf); [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)

### R-29: ai_arithmetic_gets_width_and_sign_sanitization
- statement: AI-origin arithmetic should be passed through an explicit width/sign sanitization step that inserts casts, literal sizes, and reviewed truncation points, because under-specified datapaths are a predictable failure class for code generators working without a hardware-native type discipline ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266), [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf), [Verilator warnings](https://verilator.org/guide/latest/warnings.html), [verible-verilog-lint](https://chipsalliance.github.io/verible/verilog_lint.html)).
- kind: heuristic
- strength: high
- applies_to.code_origin: [ai_generated, ai_assisted]
- when: when generated RTL contains arithmetic on mixed-width operands, shifts, concatenations, or implicit signed operations.
- unless: unless all such expressions are already normalized by a trusted template library.
- predicts: [(sim.numeric_mismatch_risk, direction, up), (synth.width_warnings, direction, up)]
- prevents: silent truncation and sign-extension bugs in generated datapaths; rough cost saved 2-12 engineer-hours.
- rationale: The LLM-RTL error paper directly attributes many failures to domain-knowledge gaps rather than generic reasoning limits, and arithmetic sizing is one of the sharpest hardware-specific knowledge tests ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266)). Verilator and Verible already encode the exact static checks needed here, so the operational fix is mostly process, not new tooling ([Verilator warnings](https://verilator.org/guide/latest/warnings.html), [verible-verilog-lint](https://chipsalliance.github.io/verible/verilog_lint.html)).
- citations: [Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266); [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf); [Verilator warnings](https://verilator.org/guide/latest/warnings.html); [verible-verilog-lint](https://chipsalliance.github.io/verible/verilog_lint.html)

### R-30: ai_output_must_resolve_to_real_language_and_library_primitives
- statement: AI-generated RTL should be rejected if it invents primitives, mixes unsupported Verilog-2001 idioms into an SV-only codebase, or otherwise emits syntax that no agreed toolchain/library understands, because generated code quality improves only when external rule-checking and domain-specific retrieval constrain the model to real constructs ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266), [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf), [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html), [2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html)).
- kind: constraint
- strength: high
- applies_to.code_origin: [ai_generated, ai_assisted]
- when: when the model introduces new instances, macros, coding idioms, or language features beyond an approved project template set.
- unless: unless the emitted primitive is resolved against a known technology/library manifest.
- predicts: [(synth.elaboration_failure_risk, direction, up), (rtl.toolchain_portability, direction, down)]
- prevents: non-compiling generated code and fabricated dependency chains; rough cost saved 2-16 engineer-hours.
- rationale: The 2025 error analysis argues that retrieval-augmented domain knowledge and rule-checking are effective precisely because the base model does not reliably know the RTL environment on its own ([Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266)). OpenTitan and Yosys together give a practical alternative: define a narrow supported language/style subset and lint everything against it ([OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html), [2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html)).
- citations: [Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266); [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf); [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html); [2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html)

## (G) DFT readiness

### R-31: async_controls_are_test_controllable
- statement: Asynchronous set/reset controls used in functional RTL should be designed so they can be overridden or disabled during scan shift and re-enabled during capture, because uncontrollable async controls directly reduce test coverage, create reset-domain hazards, and can corrupt scan behavior ([DFT architectural tips: testing of asynchronous sets/resets](https://blogs.sw.siemens.com/tessent/2019/07/24/dft-architectural-tips-testing-of-asynchronous-sets-resets/), [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/), [Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf)).
- kind: constraint
- strength: high
- applies_to.code_origin: any
- when: when async reset/set exists below top-level IO or is locally generated.
- unless: unless DFT architecture already inserts wrapper/control logic that guarantees controllability.
- predicts: [(synth.scan_readiness, direction, up), (synth.async_control_test_coverage, direction, up)]
- prevents: scan corruption and lost ATPG coverage; rough cost saved 1-2 DFT closure iterations.
- rationale: Tessent’s public guidance is unambiguous: async sets/resets must be defined, checked for controllability, disabled during shift, and enabled during capture ([DFT architectural tips: testing of asynchronous sets/resets](https://blogs.sw.siemens.com/tessent/2019/07/24/dft-architectural-tips-testing-of-asynchronous-sets-resets/)). Siemens’ 2024 RDC guidance adds the modern systems reason to care early: reset behavior is now a dedicated signoff concern, so test controllability and safe reset release should be designed together rather than patched late ([Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)).
- citations: [DFT architectural tips: testing of asynchronous sets/resets](https://blogs.sw.siemens.com/tessent/2019/07/24/dft-architectural-tips-testing-of-asynchronous-sets-resets/); [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/); [Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf)

### R-32: dft_clean_rtl_has_no_uncontrolled_loops_or_hidden_async_paths
- statement: DFT-ready RTL should avoid combinational loops and should make every asynchronous control path obvious, bounded, and reviewable, because loops impede stable scan behavior while hidden async paths create both test and functional uncertainty ([Verilator warnings](https://verilator.org/guide/latest/warnings.html), [DFT architectural tips: testing of asynchronous sets/resets](https://blogs.sw.siemens.com/tessent/2019/07/24/dft-architectural-tips-testing-of-asynchronous-sets-resets/), [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)).
- kind: constraint
- strength: medium
- applies_to.code_origin: any
- when: when adding feedback control logic, reset gating, or local asynchronous qualification logic.
- unless: unless the structure is a reviewed hardened primitive with DFT signoff.
- predicts: [(synth.scan_blocker_risk, direction, down), (sim.unoptflat_loop_risk, direction, down)]
- prevents: ATPG blockage, unstable scan observations, and debug ambiguity; rough cost saved 4-16 engineer-hours plus DFT clean-up.
- rationale: Verilator’s UNOPTFLAT warning is not a DFT checker, but it is a strong early signal that the logic structure is harder to optimize and reason about because of feedback or loop-like behavior ([Verilator warnings](https://verilator.org/guide/latest/warnings.html)). Tessent and OpenTitan together provide the operational message: make asynchronous behavior explicit and lint-clean at RTL, not after scan insertion has already become complicated ([DFT architectural tips: testing of asynchronous sets/resets](https://blogs.sw.siemens.com/tessent/2019/07/24/dft-architectural-tips-testing-of-asynchronous-sets-resets/), [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)).
- citations: [Verilator warnings](https://verilator.org/guide/latest/warnings.html); [DFT architectural tips: testing of asynchronous sets/resets](https://blogs.sw.siemens.com/tessent/2019/07/24/dft-architectural-tips-testing-of-asynchronous-sets-resets/); [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)

## (H) Power / clock-gating signals visible at RTL

### R-33: enable_inside_alwaysff_is_the_default_gating_surface
- statement: The preferred RTL surface for clock-gating inference is the ordinary enable-inside-`always_ff` pattern, because it preserves clean clock semantics while still exposing shared conditional update behavior to synthesis and RTL power tools ([PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html), [Converting Clock Gating to Clock Enable - 2025.1 English - AMD](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Converting-Clock-Gating-to-Clock-Enable), [Reducing Power Hot Spots through RTL optimization techniques](https://www.design-reuse.com/article/61502-reducing-power-hot-spots-through-rtl-optimization-techniques/)).
- kind: heuristic
- strength: high
- applies_to.code_origin: any
- when: when a flop or register bank only updates on a valid, write-enable, or active-state condition.
- unless: unless a technology-specific ICG instantiation is intentionally required.
- predicts: [(synth.icg_insertion_opportunity, direction, up), (synth.clock_power, direction, down), (sta.clock_analysis_clarity, direction, up)]
- prevents: needlessly opaque power intent and manual clock-path editing; rough cost saved 4-12 engineer-hours during power cleanup.
- rationale: PrimePower RTL explicitly explores clock-gating and glitch-power opportunities from RTL, so visible enable structure is part of the analyzable design intent ([PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html)). AMD recommends clock-enable conversion over direct gating, and Design-Reuse shows the exact enable-coded pattern used for tool-inferred gating at RTL ([Converting Clock Gating to Clock Enable - 2025.1 English - AMD](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Converting-Clock-Gating-to-Clock-Enable), [Reducing Power Hot Spots through RTL optimization techniques](https://www.design-reuse.com/article/61502-reducing-power-hot-spots-through-rtl-optimization-techniques/)).
- citations: [PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html); [Converting Clock Gating to Clock Enable - 2025.1 English - AMD](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Converting-Clock-Gating-to-Clock-Enable); [Reducing Power Hot Spots through RTL optimization techniques](https://www.design-reuse.com/article/61502-reducing-power-hot-spots-through-rtl-optimization-techniques/)

### R-34: shared_low_activity_enables_are_better_than_hand_gated_clocks
- statement: If power intent is visible at RTL, prefer shared, local, low-activity enable conditions that can gate a meaningful group of registers over ad-hoc hand-gated clocks, because modern gating flows look for minimal safe conditions and may skip insertion when too few flops benefit ([Implementing automatic clock gating in the OpenROAD ASIC design toolchain](https://antmicro.com/blog/2025/07/automatic-clock-gating-in-openroad/), [Clock Gating | Home](https://24x7fpga.com/rtl_directory/2024_09_13_12_36_11_clock_gating/), [PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html)).
- kind: heuristic
- strength: medium
- applies_to.code_origin: any
- when: when several neighboring registers share a common functional idle condition.
- unless: unless gating a very small group is still justified by measured activity.
- predicts: [(rtl.clock_gating_candidate_group_size, direction, up), (synth.icg_insertion_success, direction, up), (synth.power, direction, down)]
- prevents: fragile custom gating logic and low-value gating attempts that never survive synthesis; rough cost saved 4-12 engineer-hours of power-tuning churn.
- rationale: Antmicro’s OpenROAD gating flow explicitly uses heuristics around related nets, signal activity, and a minimum gated-register threshold before inserting a gate, which is a practical codification of “visible and shareable enable first” ([Implementing automatic clock gating in the OpenROAD ASIC design toolchain](https://antmicro.com/blog/2025/07/automatic-clock-gating-in-openroad/)). The 24x7FPGA note explains the electrical downside of naive logic-gated clocks, while PrimePower shows why keeping intent in enables is better for early power reasoning ([Clock Gating | Home](https://24x7fpga.com/rtl_directory/2024_09_13_12_36_11_clock_gating/), [PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html)).
- citations: [Implementing automatic clock gating in the OpenROAD ASIC design toolchain](https://antmicro.com/blog/2025/07/automatic-clock-gating-in-openroad/); [Clock Gating | Home](https://24x7fpga.com/rtl_directory/2024_09_13_12_36_11_clock_gating/); [PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html)

## Sources consulted

- [Shift-Left Techniques in Electronic Design Automation: A Survey](https://arxiv.org/html/2509.14551v1)
- [Understanding and Mitigating Errors of LLM-Generated RTL Code](https://arxiv.org/abs/2508.05266)
- [Automatically Fix RTL Lint Violations with GenAI](https://dvcon-proceedings.org/wp-content/uploads/3.4-yePyp1ZXiOnS-DVCon_Taiwan_2025_paper_2-1.pdf)
- [Annotating Slack Directly on Your Verilog: Fine-Grained RTL Timing Evaluation for Early Optimization](https://arxiv.org/abs/2403.18453)
- [Bridging Layout and RTL: Knowledge Distillation based Timing Prediction](https://proceedings.mlr.press/v267/wang25dn.html)
- [MasterRTL: A Pre-Synthesis PPA Estimation Framework for Any RTL Design](https://arxiv.org/abs/2311.08441)
- [CircuitFusion: Multimodal Hardware Circuit Representation Learning](https://arxiv.org/html/2505.02168v1)
- [Optimizing the RTL Design Flow with Real-Time PPA Analysis](https://www.synopsys.com/blogs/chip-design/optimizing-rtl-design-flow-real-time-ppa-analysis.html)
- [PrimePower: RTL to Signoff Power Analysis](https://www.synopsys.com/implementation-and-signoff/signoff/primepower.html)
- [Verilator warnings](https://verilator.org/guide/latest/warnings.html)
- [verible-verilog-lint](https://chipsalliance.github.io/verible/verilog_lint.html)
- [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md)
- [OpenTitan hardware methodology](https://opentitan.org/book/doc/contributing/hw/methodology.html)
- [2. Basic principles — YosysHQ Docs](https://yosyshq.readthedocs.io/projects/yosys/en/0.39/CHAPTER_Basics.html)
- [Memory handling — YosysHQ Yosys 0.43 documentation](https://yosyshq.readthedocs.io/projects/yosys/en/0.43/using_yosys/synthesis/memory.html)
- [Clock Domain Crossing (CDC) Design & Verification Techniques Using SystemVerilog](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/04.SystemVerilog/2008-Clock%20Domain%20Crossing%20(CDC)%20Design%20&%20Verification%20Techniques%20Using%20SystemVerilog.pdf)
- [Synchronous Resets? Asynchronous Resets? I am so confused...](https://lcdm-eng.com/papers/snug02_Resets1.pdf)
- [A Solution to Verilog's "full_case" & "parallel_case" Evil Twins](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/1Sunburst%20Design/SystemVerilog's%20priority%20&%20unique%20-%20A%20Solution%20to%20Verilog's%20full_case%20&%20parallel_case%20Evil%20Twins!.pdf)
- [Yet Another Latch and Gotchas Paper](https://lcdm-eng.com/papers/snug12_Paper_final.pdf)
- [One-hot State Machine in SystemVerilog – Reverse Case Statement](https://www.verilogpro.com/systemverilog-one-hot-state-machine/)
- [State Machine Coding Styles for Synthesis](http://staff.ustc.edu.cn/~wyu0725/FPGA/snug_collection/Clifford%20E.%20Cummings'%20Paper/02.FSM/1998-State%20Machine%20Coding%20Styles%20for%20Synthesis.pdf)
- [OpenROAD documentation](https://openroad.readthedocs.io/en/latest/main/README.html)
- [Implementing automatic clock gating in the OpenROAD ASIC design toolchain](https://antmicro.com/blog/2025/07/automatic-clock-gating-in-openroad/)
- [Addressing Congestion - 2025.1 English - UG949](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Addressing-Congestion)
- [Converting Clock Gating to Clock Enable - 2025.1 English - AMD](https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/Converting-Clock-Gating-to-Clock-Enable)
- [RAM_STYLE - 2024.1 English - UG912](https://docs.amd.com/r/2024.1-English/ug912-vivado-properties/RAM_STYLE)
- [Choosing Between Distributed RAM and Dedicated Block RAM - 2024.2 English](https://docs.amd.com/r/2024.2-English/ug901-vivado-synthesis/Choosing-Between-Distributed-RAM-and-Dedicated-Block-RAM?contentId=P5lJu6Y5~LTu~iWW~10fBQ)
- [Running Synthesis with Tcl - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/Running-Synthesis-with-Tcl)
- [USE_DSP - 2024.1 English - UG901](https://docs.amd.com/r/2024.1-English/ug901-vivado-synthesis/USE_DSP)
- [Navigating Reset Domain Crossings to Safety in Complex SoCs](https://blogs.sw.siemens.com/verificationhorizons/2024/05/21/navigating-reset-domain-crossings-to-safety-in-complex-socs/)
- [DFT architectural tips: testing of asynchronous sets/resets](https://blogs.sw.siemens.com/tessent/2019/07/24/dft-architectural-tips-testing-of-asynchronous-sets-resets/)
- [Clock Gating | Home](https://24x7fpga.com/rtl_directory/2024_09_13_12_36_11_clock_gating/)
- [Reducing Power Hot Spots through RTL optimization techniques](https://www.design-reuse.com/article/61502-reducing-power-hot-spots-through-rtl-optimization-techniques/)
- [ibex/README.md at master · lowRISC/ibex](https://github.com/lowRISC/ibex/blob/master/README.md)
- [Pipeline Details - Ibex](https://ibex-core.readthedocs.io/en/latest/03_reference/pipeline_details.html)
- [PRIMAL: Power Inference using Machine Learning](https://research.nvidia.com/sites/default/files/pubs/2019-06_PRIMAL:-Power-Inference/24_1_Zhou_PRIMAL.pdf)
- [Synopsys 2026 design workflow announcement](https://news.synopsys.com/2026-03-11-Synopsys-Outlines-Vision-for-Engineering-the-Future)
